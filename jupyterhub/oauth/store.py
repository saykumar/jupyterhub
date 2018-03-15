"""Utilities for hooking up oauth2 to JupyterHub's database

implements https://python-oauth2.readthedocs.io/en/latest/store.html
"""

import threading

from oauth2.datatype import Client, AuthorizationCode
from oauth2.error import AuthCodeNotFound, ClientNotFoundError, UserNotAuthenticated
from oauth2.grant import AuthorizationCodeGrant
from oauth2.web import AuthorizationCodeGrantSiteAdapter
import oauth2.store
from oauth2 import Provider
from oauth2.tokengenerator import Uuid4 as UUID4

from sqlalchemy.orm import scoped_session
from tornado.escape import url_escape
from tornado.log import app_log

from .. import orm
from ..utils import url_path_join, hash_token, compare_token


class JupyterHubSiteAdapter(AuthorizationCodeGrantSiteAdapter):
    """
    This adapter renders a confirmation page so the user can confirm the auth
    request.
    """
    def __init__(self, login_url):
        self.login_url = login_url

    def render_auth_page(self, request, response, environ, scopes, client):
        """Auth page is a redirect to login page"""
        response.status_code = 302
        response.headers['Location'] = self.login_url + '?next={}'.format(
            url_escape(request.handler.request.path + '?' + request.handler.request.query)
        )
        return response

    def authenticate(self, request, environ, scopes, client):
        app_log.info("[JHSiteAdapter] Authenticating user request.")
        app_log.info("[JHSiteAdapter] Request: %s, client: %s",
                     request.__dict__, client)
        handler = request.handler
        app_log.info("[JHSiteAdapter] request handler: %s", handler)
        app_log.info("[JHSiteAdapter] Getting current user from handler")
        user = handler.get_current_user()
        app_log.info("[JHSiteAdapter] current user from handler: %s", user)
        if user:
            app_log.info("[JHSiteAdapter] Returning authenticated user: %s",
                         user.id)
            return {}, user.id
        else:
            raise UserNotAuthenticated()

    def user_has_denied_access(self, request):
        # user can't deny access
        return False
    

class HubDBMixin(object):
    """Mixin for connecting to the hub database"""
    def __init__(self, session_factory):
        self.db = session_factory()


class AccessTokenStore(HubDBMixin, oauth2.store.AccessTokenStore):
    """OAuth2 AccessTokenStore, storing data in the Hub database"""

    def save_token(self, access_token):
        """
        Stores an access token in the database.

        :param access_token: An instance of :class:`oauth2.datatype.AccessToken`.

        """
        
        app_log.info("[AccessTokenStore] Saving access token %s :: %s",
                     access_token.user_id, access_token.token)
        user = self.db.query(orm.User).filter(orm.User.id == access_token.user_id).first()
        if user is None:
            raise ValueError("No user for access token: %s" % access_token.user_id)
        app_log.info("[AccessTokenStore] About to save token %s for user %s",
                     access_token.token, user)
        orm_access_token = orm.OAuthAccessToken(
            client_id=access_token.client_id,
            grant_type=access_token.grant_type,
            expires_at=access_token.expires_at,
            refresh_token=access_token.refresh_token,
            refresh_expires_at=access_token.refresh_expires_at,
            token=access_token.token,
            user=user,
        )
        self.db.add(orm_access_token)
        try:
            self.db.commit()
        except:
            self.db.rollback()
        app_log.info("[AccessTokenStore] Token %s saved for user %s, client %s",
                     orm_access_token, orm_access_token.user, orm_access_token.client_id)


class AuthCodeStore(HubDBMixin, oauth2.store.AuthCodeStore):
    """
    OAuth2 AuthCodeStore, storing data in the Hub database
    """
    def fetch_by_code(self, code):
        """
        Returns an AuthorizationCode fetched from a storage.

        :param code: The authorization code.
        :return: An instance of :class:`oauth2.datatype.AuthorizationCode`.
        :raises: :class:`oauth2.error.AuthCodeNotFound` if no data could be retrieved for
                 given code.

        """
        app_log.info("[AuthCodeStore] Retrieving authorization code: %s", code)
        try:
            orm_code = self.db\
                .query(orm.OAuthCode)\
                .filter(orm.OAuthCode.code == code)\
                .first()
        except:
            self.db.rollback()
        app_log.info("[AuthCodeStore] ORM code: %s", orm_code)
        if orm_code is None:
            raise AuthCodeNotFound()
        else:
            authorization_code = AuthorizationCode(
                client_id=orm_code.client_id, code=code,
                expires_at=orm_code.expires_at,
                redirect_uri=orm_code.redirect_uri, scopes=[],
                user_id=orm_code.user_id, )

            app_log.info("[AuthCodeStore] Returning auth code: %s", authorization_code)
            return authorization_code


    def save_code(self, authorization_code):
        """
        Stores the data belonging to an authorization code token.

        :param authorization_code: An instance of
                                   :class:`oauth2.datatype.AuthorizationCode`.
        """
        app_log.info("[AuthCodeStore] Saving authorization code.")
        app_log.info("[AuthCodeStore] Saving auth code: client %s, user %s --> code %s",
                     authorization_code.client_id,
                     authorization_code.user_id, authorization_code.code)
        orm_code = orm.OAuthCode(
            client_id=authorization_code.client_id,
            code=authorization_code.code,
            expires_at=authorization_code.expires_at,
            user_id=authorization_code.user_id,
            redirect_uri=authorization_code.redirect_uri,
        )
        self.db.add(orm_code)
        try:
            self.db.commit()
        except:
            self.db.rollback()
        app_log.info("[AuthCodeStore] Saved ORM auth code: %s", orm_code)


    def delete_code(self, code):
        """
        Deletes an authorization code after its use per section 4.1.2.

        http://tools.ietf.org/html/rfc6749#section-4.1.2

        :param code: The authorization code.
        """
        app_log.info("[AuthCodeStore] Deleting code %s", code)
        orm_code = self.db.query(orm.OAuthCode).filter(orm.OAuthCode.code == code).first()
        if orm_code is not None:
            self.db.delete(orm_code)
            try:
                self.db.commit()
            except:
                self.db.rollback()
            app_log.info("[AuthCodeStore] Deleted code %s", orm_code)



class HashComparable:
    """An object for storing hashed tokens

    Overrides `==` so that it compares as equal to its unhashed original

    Needed for storing hashed client_secrets
    because python-oauth2 uses::

        secret == client.client_secret

    and we don't want to store unhashed secrets in the database.
    """
    def __init__(self, hashed_token):
        self.hashed_token = hashed_token
    
    def __repr__(self):
        return "<{} '{}'>".format(self.__class__.__name__, self.hashed_token)

    def __eq__(self, other):
        return compare_token(self.hashed_token, other)


class ClientStore(HubDBMixin, oauth2.store.ClientStore):
    """OAuth2 ClientStore, storing data in the Hub database"""

    def fetch_by_client_id(self, client_id):
        """Retrieve a client by its identifier.

        :param client_id: Identifier of a client app.
        :return: An instance of :class:`oauth2.datatype.Client`.
        :raises: :class:`oauth2.error.ClientNotFoundError` if no data could be retrieved for
                 given client_id.
        """
        app_log.info("[ClientStore] Fetching client for id %s", client_id)
        try:
            orm_client = self.db\
                .query(orm.OAuthClient)\
                .filter(orm.OAuthClient.identifier == client_id)\
                .first()
        except:
            self.db.rollback()
        app_log.info("[ClientStore] ORM client for id %s: %s", client_id, orm_client)
        if orm_client is None:
            raise ClientNotFoundError()
        app_log.info("[ClientStore] ORM client redirect uri: %s", orm_client.redirect_uri)
        return Client(identifier=client_id,
                      redirect_uris=[orm_client.redirect_uri],
                      secret=HashComparable(orm_client.secret),
                      )

    def add_client(self, client_id, client_secret, redirect_uri):
        """Add a client

        hash its client_secret before putting it in the database.
        """
        app_log.info("[ClientStore] Creating new client with id %s, redirect uri %s",
                     client_id, redirect_uri)
        # clear existing clients with same ID
        try:
            for client in self.db\
                    .query(orm.OAuthClient)\
                    .filter(orm.OAuthClient.identifier == client_id):
                self.db.delete(client)
            self.db.commit()

            orm_client = orm.OAuthClient(
                identifier=client_id,
                secret=hash_token(client_secret),
                redirect_uri=redirect_uri,
            )
            self.db.add(orm_client)
            self.db.commit()
        except:
            self.db.rollback()
        app_log.info("[ClientStore] Saved orm client: %s", orm_client)


def make_provider(session_factory, url_prefix, login_url):
    """Make an OAuth provider"""
    token_store = AccessTokenStore(session_factory)
    code_store = AuthCodeStore(session_factory)
    client_store = ClientStore(session_factory)
    
    provider = Provider(
        access_token_store=token_store,
        auth_code_store=code_store,
        client_store=client_store,
        token_generator=UUID4(),
    )
    app_log.info("[MakeProvider] Created OAuth provider %s", provider)
    provider.token_path = url_path_join(url_prefix, 'token')
    provider.authorize_path = url_path_join(url_prefix, 'authorize')
    site_adapter = JupyterHubSiteAdapter(login_url=login_url)
    provider.add_grant(AuthorizationCodeGrant(site_adapter=site_adapter))
    return provider

