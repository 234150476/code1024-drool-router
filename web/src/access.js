// The dashboard credential is separate from the gateway administrator key.
// Bearer headers work inside cross-site iframes without third-party cookies.
const KEY = 'drool-dashboard-access';
const HOST_KEY = 'drool-host-session';
const initial = new URL(typeof location !== 'undefined' ? location.href : 'http://localhost/');
const hostToken = initial.searchParams.get('token');
if (hostToken) {
  try { sessionStorage.setItem(HOST_KEY, hostToken); } catch { /* Memory below. */ }
  initial.searchParams.delete('token');
  initial.searchParams.delete('user_id');
  history.replaceState(null, '', initial.pathname + initial.search + initial.hash);
}
export function accessToken() {
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
}
export function saveAccessToken(token) {
  try { sessionStorage.setItem(KEY, token.trim()); } catch { /* Memory fallback below. */ }
  memoryToken = token.trim();
}
let memoryToken = '';
export async function apiFetch(url, options = {}) {
  let inherited = hostToken || '';
  try { inherited = sessionStorage.getItem(HOST_KEY) || inherited; } catch { /* Optional storage. */ }
  const token = memoryToken || accessToken() || inherited;
  const headers = new Headers(options.headers);
  if (token) headers.set('Authorization', 'Bearer ' + token);
  return fetch(url, {...options, headers, credentials: 'omit'});
}
export function hostSession() {
  try { return sessionStorage.getItem(HOST_KEY) || hostToken || ''; } catch { return hostToken || ''; }
}
export function adminFetch(url, options = {}) {
  const headers = new Headers(options.headers);
  const token = hostSession();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  return fetch(url, {...options, headers, credentials: 'omit'});
}
