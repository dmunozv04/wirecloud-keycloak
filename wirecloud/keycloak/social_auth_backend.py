# -*- coding: utf-8 -*-

# Copyright (c) 2019-2021 Future Internet Consulting and Development Solutions S.L.

# This file is part of Wirecloud Keycloak plugin.

# Wirecloud is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Wirecloud is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with Wirecloud.  If not, see <http://www.gnu.org/licenses/>.

import logging
from urllib.parse import urljoin

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import get_language
from jose import jwk, jwt
from social_core.backends.open_id_connect import OpenIdConnectAuth

from wirecloud.keycloak.utils import get_user_model, get_group_model

# Get an instance of a logger
logger = logging.getLogger(__name__)

KEYCLOAK_OIDC_ENDPOINT = 'realms/{}'


class KeycloakOpenIdConnect(OpenIdConnectAuth):
    """Keycloak IDM OAuth authentication endpoint"""

    name = 'keycloak_oidc'
    ID_KEY = 'preferred_username'
    DEFAULT_SCOPE = ["openid", "profile", "email", "offline_access"]

    URL = getattr(settings, 'SOCIAL_AUTH_KEYCLOAK_OIDC_URL', '')
    REALM = getattr(settings, 'SOCIAL_AUTH_KEYCLOAK_OIDC_REALM', '')
    END_SESSION_URL = ''
    JWT_DECODE_OPTIONS = {'verify_at_hash': False}
    EXTRA_DATA = [
        'id_token',
        'username',
        'refresh_token',
        'session_state',
        ('expires_in', 'expires'),
        ('sub', 'id'),
        'roles'
    ]

    def __init__(self, *args, **kwargs):
        super(KeycloakOpenIdConnect, self).__init__(*args, **kwargs)
        self.OIDC_ENDPOINT = urljoin(self.URL, KEYCLOAK_OIDC_ENDPOINT.format(self.REALM))

    def end_session_url(self):
        return self.END_SESSION_URL or \
            self.oidc_config().get('end_session_endpoint')

    def auth_complete_params(self, state=None):
        params = super(KeycloakOpenIdConnect, self).auth_complete_params(state)
        session = self.strategy.request.session
        # Force new session key
        session.cycle_key()
        # Disable session key cycling on authentication
        session.cycle_key = lambda: None
        session.flush = session.clear
        # Send new session id to Keycloak
        logger.debug("requesting openid connect credentials associated with session {}".format(session.session_key))
        params["client_session_state"] = session.session_key
        return params

    def auth_extra_arguments(self):
        params = super(KeycloakOpenIdConnect, self).auth_extra_arguments() or {}
        params['kc_locale'] = get_language()
        return params

    def get_user_details(self, response):
        """Return user details from the returned userinfo endpoint and from the id_token"""

        global_role = getattr(settings, 'SOCIAL_AUTH_KEYCLOAK_OIDC_GLOBAL_ROLE', False)
        roles = []

        if global_role:
            roles = self.id_token.get('realm_access', {}).get('roles', [])
        else:
            client_id, client_secret = self.get_key_and_secret()
            roles = self.id_token.get('resource_access', {}).get(client_id, {}).get('roles', [])

        superuser = any(role.strip().lower() == "admin" for role in roles)
        group_roles = [role.strip().lower() for role in roles]

        username_key = self.setting('USERNAME_KEY', default=self.USERNAME_KEY)
        return {
            'username': response.get(username_key),
            'email': response.get('email') or '',
            'fullname': response.get('name') or '',
            'first_name': response.get('given_name') or '',
            'last_name': response.get('family_name') or '',
            'is_superuser': superuser,
            'is_staff': superuser,
            'roles': group_roles
        }

    def parse_incomming_data(self, data):
        key = self.find_valid_key(data)
        rsakey = jwk.construct(key)
        return jwt.decode(data, rsakey.to_pem().decode('utf-8'), algorithms=key['alg'], options={"verify": False})


@receiver(post_save, sender=get_user_model())
def add_user_groups(sender, instance, created, **kwargs):
    if instance.social_auth.count() > 0:
        social = instance.social_auth.all()[0]
        # Remove user groups to support removed roles
        instance.groups.clear()

        # Add user to role groups
        if 'roles' in social.extra_data:
            for role in social.extra_data['roles']:
                group_model = get_group_model()
                role_group, created = group_model.objects.get_or_create(name=role.strip().lower())
                instance.groups.add(role_group)
