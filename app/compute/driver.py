"""Creating and destroying the machine that does the rendering.

THE ONLY CODE IN THIS APPLICATION THAT SPENDS MONEY. Everything else costs
what it costs; this decides how many machines exist. That shapes every
choice here:

  * `destroy` re-reads the label from the provider immediately before
    deleting, and refuses anything not named `vsw-compute…`. The restricted
    user is the first guard — it cannot see the web box at all — and this is
    the second, because a user's grants can be widened later by somebody who
    has forgotten that this code assumed otherwise.
  * `create` is rate-limited by the caller against a ceiling. A loop that
    creates machines is not a bug that costs a wasted afternoon; it is a
    bug that costs whatever the account's limit allows.
  * nothing here retries a create. A create that times out may still have
    succeeded, and retrying it is how one machine becomes three. The caller
    reconciles by listing instead.

`FakeDriver` exists so every rule above is tested without an account, a
network, or a cent.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

API = "https://api.linode.com/v4"

# Every machine this application creates is named with this prefix, and
# nothing without it is ever deleted. Short, because a label is also what an
# operator reads in a billing line at the end of the month.
LABEL_PREFIX = "vsw-compute"


class ComputeError(RuntimeError):
    """The provider refused, or answered something unusable."""


@dataclass(frozen=True)
class Machine:
    id: int
    label: str
    status: str
    ipv4: str = ""

    @property
    def is_ours(self) -> bool:
        return self.label.startswith(LABEL_PREFIX)


class Driver(Protocol):
    def create(self, label: str, plan: str, image: str, region: str,
               user_data: str, ssh_keys: list[str],
               interfaces: list[dict[str, Any]] | None = None) -> Machine: ...
    def find_ours(self) -> list[Machine]: ...
    def get(self, machine_id: int) -> Machine | None: ...
    def destroy(self, machine_id: int) -> bool: ...


class LinodeDriver:
    """The real one."""

    def __init__(self, token: str, timeout: float = 60.0) -> None:
        if not token:
            raise ComputeError("no LINODE_TOKEN, so no machine can be made")
        self._token = token
        self._timeout = timeout

    # -- plumbing --------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{API}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = "; ".join(e.get("reason", "?")
                                   for e in json.load(exc).get("errors", []))
            except Exception:                          # noqa: BLE001
                detail = exc.reason or ""
            raise ComputeError(f"{method} {path}: {exc.code} {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise ComputeError(f"{method} {path}: {exc}") from exc

    @staticmethod
    def _machine(row: dict) -> Machine:
        return Machine(id=int(row["id"]), label=row.get("label", ""),
                       status=row.get("status", ""),
                       ipv4=(row.get("ipv4") or [""])[0])

    # -- the three things that matter ------------------------------------
    def create(self, label: str, plan: str, image: str, region: str,
               user_data: str, ssh_keys: list[str],
               interfaces: list[dict[str, Any]] | None = None) -> Machine:
        """One machine. NOT retried — see the module docstring."""
        if not label.startswith(LABEL_PREFIX):
            raise ComputeError(
                f"refusing to create {label!r}: every machine this makes must "
                f"be named {LABEL_PREFIX}… so that `destroy` can tell its own "
                f"work from somebody else's")
        body: dict[str, Any] = {
            "region": region,
            "type": plan,
            "label": label,
            "image": image,
            # BASE64. The Metadata service requires user_data base64-encoded;
            # raw #cloud-config text is rejected 400, and the machine either
            # never gets created or boots with no config. This is the one
            # channel the node's .env and broker password travel through.
            "metadata": {
                "user_data": base64.b64encode(
                    user_data.encode("utf-8")).decode("ascii")},
            # An image build requires a root password even when only keys are
            # used to log in. It is random and never stored: SSH is by key
            # (authorized_keys below), and a password nobody knows locks out
            # password login rather than enabling it.
            "root_pass": secrets.token_urlsafe(32),
            # Credentials reach the machine only through user_data above: not
            # in the image, not in the repository, not on a disk anyone can
            # read afterwards.
            "authorized_keys": ssh_keys,
            "booted": True,
            "tags": ["vsw-compute"],
        }
        # The private VLAN the node reaches the broker on, plus a public NIC
        # for the object store and package mirrors. Without the VLAN the node
        # cannot see the broker at all.
        if interfaces:
            body["interfaces"] = interfaces
        row = self._call("POST", "/linode/instances", body)
        made = self._machine(row)
        logger.info("created %s (%s) as %s", made.label, plan, made.id)
        return made

    def find_ours(self) -> list[Machine]:
        """Every machine of ours the provider currently has.

        The reconciliation path. After a restart the row in `compute` may
        disagree with reality — a create that succeeded while the scaler was
        being restarted leaves a machine nothing remembers — and the
        provider is the referee.
        """
        rows = self._call("GET", "/linode/instances").get("data", [])
        return [m for m in map(self._machine, rows) if m.is_ours]

    def get(self, machine_id: int) -> Machine | None:
        try:
            return self._machine(
                self._call("GET", f"/linode/instances/{machine_id}"))
        except ComputeError:
            return None

    def destroy(self, machine_id: int) -> bool:
        """Delete, having checked WITH THE PROVIDER what it is deleting.

        The label is re-read here rather than taken from the caller. A caller
        holding a stale id — from a database row written before a restart,
        say — would otherwise delete whatever now has that id, and ids are
        reused. This is the difference between "delete the machine I made"
        and "delete number 104872575".
        """
        machine = self.get(machine_id)
        if machine is None:
            logger.info("machine %s is already gone", machine_id)
            return True
        if not machine.is_ours:
            raise ComputeError(
                f"REFUSING to delete {machine_id} ({machine.label!r}): it is "
                f"not named {LABEL_PREFIX}… and so is not ours to delete")
        self._call("DELETE", f"/linode/instances/{machine_id}")
        logger.info("destroyed %s (%s)", machine.label, machine_id)
        return True


class FakeDriver:
    """A provider that costs nothing, for the tests that matter most."""

    def __init__(self) -> None:
        self.machines: dict[int, Machine] = {}
        self.creates = 0
        self.destroys = 0
        self.refused: list[str] = []
        self._next = 1000

    def create(self, label: str, plan: str, image: str, region: str,
               user_data: str, ssh_keys: list[str],
               interfaces: list[dict] | None = None) -> Machine:
        if not label.startswith(LABEL_PREFIX):
            self.refused.append(label)
            raise ComputeError(f"refusing to create {label!r}")
        self._next += 1
        made = Machine(self._next, label, "provisioning", "203.0.113.1")
        self.machines[made.id] = made
        self.creates += 1
        self.last_interfaces = interfaces        # so a test can inspect it
        self.last_user_data = user_data
        return made

    def find_ours(self) -> list[Machine]:
        return [m for m in self.machines.values() if m.is_ours]

    def get(self, machine_id: int) -> Machine | None:
        return self.machines.get(machine_id)

    def destroy(self, machine_id: int) -> bool:
        machine = self.machines.get(machine_id)
        if machine is None:
            return True
        if not machine.is_ours:
            self.refused.append(machine.label)
            raise ComputeError(f"REFUSING to delete {machine.label!r}")
        del self.machines[machine_id]
        self.destroys += 1
        return True

    # -- for tests, so a foreign machine can be put in the way ------------
    def plant(self, label: str) -> Machine:
        self._next += 1
        made = Machine(self._next, label, "running", "203.0.113.9")
        self.machines[made.id] = made
        return made
