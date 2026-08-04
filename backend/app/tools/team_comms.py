"""One-on-one messaging with crew members (clone instances).

Every clone's backend joins the shared ``starter-app-net`` with a unique
network alias ``backend-<name>`` (see clone_tools._add_backend_net_alias).
This toolkit resolves that alias and posts a private message to the crew
member's team run endpoint, returning their reply.
"""

import json
import urllib.parse
import urllib.request

from agno.tools import Toolkit

_TIMEOUT = 300  # LLM replies can take a while


class TeamComms(Toolkit):
    """Private one-on-one chat with a crew member."""

    def __init__(self) -> None:
        super().__init__(name="team_comms", tools=[self.talk_to])

    def talk_to(self, name: str, message: str, session_name: str | None = None) -> str:
        """Send a private one-on-one message to a crew member and return their reply.

        Pass the same ``session_name`` on later calls to keep chatting in that
        conversation. It is resolved to the crew member's session id and the
        team run continues that session. If the name doesn't exist yet, a new
        conversation is created and named after it. When omitted, an anonymous
        fresh session starts.

        Args:
            name: crew member / clone name (e.g. 'nami', 'luffy', 'zoro').
            message: what to say to them.
            session_name: optional conversation name to continue or create.
        """
        base = f"http://backend-{name}:8000"
        try:
            teams = self._get_json(f"{base}/teams")
            if not teams or not teams[0].get("id"):
                return f"Error: {name} has no active team."
            team_id = teams[0]["id"]

            form = {
                "message": message,
                "stream": "false",
                "user_id": "chopper",
            }
            created_new = False
            if session_name:
                session_id = self._find_session_id(base, session_name)
                if session_id is not None:
                    form["session_id"] = session_id  # continue existing convo
                else:
                    created_new = True  # start fresh, rename after the run

            data = urllib.parse.urlencode(form).encode()
            req = urllib.request.Request(
                f"{base}/teams/{team_id}/runs",
                data=data,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                result = json.loads(r.read())

            if created_new:
                created_id = result.get("session_id")
                if created_id:
                    self._rename_session(base, created_id, session_name)
        except Exception as e:
            return f"Error: couldn't reach {name}: {e}"

        reply = (result.get("content") or "").strip()
        return f"{name}: {reply}" if reply else f"Error: {name} returned no reply."

    @staticmethod
    def _get_json(url: str) -> list | dict:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    @staticmethod
    def _find_session_id(base: str, session_name: str) -> str | None:
        """Resolve a conversation name to its session id (case-insensitive)."""
        payload = TeamComms._get_json(f"{base}/sessions?limit=100")
        target = session_name.strip().lower()
        for s in payload.get("data") or []:
            if (s.get("session_name") or "").strip().lower() == target:
                return s.get("session_id")
        return None

    @staticmethod
    def _rename_session(base: str, session_id: str, session_name: str) -> None:
        """Give a freshly created session our chosen conversation name."""
        data = json.dumps({"session_name": session_name}).encode()
        req = urllib.request.Request(
            f"{base}/sessions/{session_id}",
            data=data,
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
