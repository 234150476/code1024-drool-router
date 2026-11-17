"""Verify an embedded administrator session against the owning Sub2API service."""
import requests


def user_identity(base_url, token, user_agent='', client_ip=''):
    if not base_url or not token or len(token) > 8192 or token.count('.') != 2:
        return False
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': user_agent}
    if client_ip:
        headers['X-Real-IP'] = client_ip
        headers['X-Forwarded-For'] = client_ip
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(base_url.rstrip('/') + '/api/v1/auth/me',
                                   headers=headers, timeout=(3, 8), allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 131072:
                return False
            value = response.json()
            user = value.get('data', {})
            if (value.get('code', 0) == 0 and user.get('status') == 'active' and
                    user.get('role') in ('admin', 'user')):
                return {'id': user.get('id'), 'role': user.get('role'), 'email': user.get('email'),
                        'username': user.get('username')}
            return None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return False


def admin_identity(base_url, token, user_agent='', client_ip=''):
    identity = user_identity(base_url, token, user_agent, client_ip)
    if identity and identity.get('role') == 'admin':
        return identity
    return None if identity is not False else False


def verify_admin_session(base_url, token, user_agent='', client_ip=''):
    return bool(admin_identity(base_url, token, user_agent, client_ip))
