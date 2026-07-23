const ACTIVE_SESSION_KEY = "active_session";

export const userId = "user";

export let agnoSessionId: string | null =
  localStorage.getItem(ACTIVE_SESSION_KEY);

export function setAgnoSessionId(id: string | null) {
  agnoSessionId = id;
  if (id) localStorage.setItem(ACTIVE_SESSION_KEY, id);
  else localStorage.removeItem(ACTIVE_SESSION_KEY);
}
