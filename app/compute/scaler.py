"""The loop that decides whether a machine should exist, and makes it so.

    python -m app.compute.scaler

ONE PROCESS, ON THE WEB BOX. Being one process makes the ordinary case
trivially exclusive; the lease row in `compute` covers the case where
somebody starts a second by mistake, and the provider's label uniqueness is
the final referee.

OFF BY DEFAULT. `COMPUTE_ENABLED` must be set before anything is created.
Everything up to that point — the decision, the reasoning, the cost it would
have incurred — runs and is recorded regardless, which is what the shadow
has been doing for days. A switch somebody has to throw is a different kind
of safety from a limit one hopes holds, and this is the first code here that
can spend money without a person watching.

THREE GUARDS, and they fail in different directions on purpose:

    the switch        nothing is created at all until it is on
    the ceiling       at most COMPUTE_MAX_CREATES_PER_HOUR, whatever the
                      queue says. A loop that creates machines is not a bug
                      that costs an afternoon.
    the label         `destroy` re-reads the name from the provider and
                      refuses anything not `vsw-compute…`

RECONCILE BEFORE DECIDING. The row in `compute` is what this process
remembers; the provider is what is true. A create that succeeded while this
was being restarted leaves a machine nothing remembers, and a row saying
`wanted` after a machine was deleted by hand leaves a decision resting on a
fiction. The provider wins, every tick.
"""
from __future__ import annotations

import logging
import signal
import time

from .. import limits, notify, storage, store
from ..settings import settings
from . import cloudinit
from .driver import ComputeError, Driver, LinodeDriver, Machine

logger = logging.getLogger(__name__)

# One machine, one name. The provider enforces label uniqueness per account,
# which is the last thing standing between two scalers and two machines.
LABEL = "vsw-compute"


class Scaler:
    def __init__(self, driver: Driver | None = None) -> None:
        self._driver = driver
        self._stop = False

    # -- the provider ----------------------------------------------------
    @property
    def driver(self) -> Driver | None:
        """Built on first use, so the loop runs and reports in shadow even
        with no token configured."""
        if self._driver is None and settings.linode_token:
            self._driver = LinodeDriver(settings.linode_token)
        return self._driver

    @property
    def enabled(self) -> bool:
        return bool(settings.compute_enabled and settings.linode_token
                    and settings.compute_image)

    def why_not(self) -> str:
        if not settings.compute_enabled:
            return "COMPUTE_ENABLED is off, so nothing is created"
        if not settings.linode_token:
            return "no LINODE_TOKEN"
        if not settings.compute_image:
            return "no COMPUTE_IMAGE to boot from"
        return ""


    # -- saying so -------------------------------------------------------
    def _alarm(self, kind: str, detail: str) -> str:
        """Record a refusal or failure, and tell the operator about it.

        EVERY condition the scaler will not resolve on its own comes through
        here. A refusal nobody hears about is indistinguishable from a
        failure: the queue simply stops moving and the first anybody knows
        is a visitor asking where their video went.

        Deduped in the database rather than in memory, so a scaler that
        restarts in a loop does not send the same message on every start —
        and so the hour between repeats survives the restart too.
        """
        logger.error("%s: %s", kind, detail)
        try:
            if store.compute_alarm(kind) and limits.allowed("mail_compute", "all"):
                notify.send_alarm(kind, detail)
        except Exception:                                # noqa: BLE001
            # Never allowed to mask the condition it is reporting.
            logger.warning("could not send the alarm for %s", kind,
                           exc_info=True)
        return f"{kind}: {detail}"

    # -- what a machine is told at birth ---------------------------------
    def _user_data(self) -> str:
        source = ""
        env_file = settings.work_dir.parent / ".env"
        if env_file.is_file():
            source = env_file.read_text(encoding="utf-8")

        password = ""
        pass_file = settings.work_dir.parent / "compute-broker.pass"
        if pass_file.is_file():
            password = pass_file.read_text(encoding="utf-8").strip()

        broker = (f"amqp://vsw-compute:{password}@{settings.compute_broker_host}"
                  f":5672/vsw")
        return cloudinit.user_data(cloudinit.environment(source, broker))

    # -- one tick --------------------------------------------------------
    def tick(self) -> dict:
        """Reconcile, decide, act. Returns what it saw and did."""
        live = self._reconcile()
        shadow = store.compute_tick(settings.compute_grace_seconds,
                                    settings.compute_keep_if_arrivals)
        wanted = shadow["state"] == "wanted"

        # There is actual work, right now.
        busy = (shadow["ready"] + shadow["unacked"]) > 0

        acted = ""
        if wanted and not live and busy:
            acted = self._create()
        elif not wanted and live:
            acted = self._destroy(live)
        # `wanted` with no machine and NO WORK is not a reason to create one.
        # The hour-aligned rule holds the state at `wanted` through the hour
        # already paid for, which is right for KEEPING a machine and
        # meaningless without one — it would otherwise create a machine to
        # sit idle until the boundary it was waiting for.

        # A tick that got all the way here is a working tick. Clearing
        # explicitly means the NEXT failure is reported immediately rather
        # than waiting out the hour between repeats.
        was = store.compute_alarm_cleared()
        if was:
            logger.info("recovered from: %s", was)

        return {**shadow, "machine": live.id if live else None, "did": acted}

    def _reconcile(self) -> Machine | None:
        """What the PROVIDER says exists, not what we remember."""
        if self.driver is None:
            return None
        try:
            ours = self.driver.find_ours()
        except ComputeError as exc:
            # Unknown is not "none". Returning None here would look like
            # "no machine exists" and create a second one.
            self._alarm(
                "cannot reach the provider",
                f"Listing machines failed: {exc}. Nothing has been created "
                f"or deleted — an API failure and an empty account look the "
                f"same to the decision, and treating one as the other is how "
                f"a second machine gets made alongside a render.")
            raise
        if len(ours) > 1:
            names = ", ".join(f"{m.label} ({m.id})" for m in ours)
            self._alarm(
                "more than one compute machine",
                f"{len(ours)} machines exist where there should be at most "
                f"one: {names}. One of them may be rendering somebody's "
                f"video, and there is no way to tell which — so nothing has "
                f"been deleted. Delete the idle one by hand in Cloud "
                f"Manager, and the scaler will carry on.")
            raise ComputeError("more than one compute machine exists")
        return ours[0] if ours else None

    def _create(self) -> str:
        if not self.enabled:
            return f"would create ({self.why_not()})"
        made = store.compute_creates_this_hour()
        if made >= settings.compute_max_creates_per_hour:
            return self._alarm(
                "hourly create ceiling reached",
                f"{made} machines have been created in the last hour and the "
                f"ceiling is {settings.compute_max_creates_per_hour}. No more "
                f"will be created until the hour rolls. Either traffic is "
                f"genuinely that busy, or something is creating and losing "
                f"machines in a loop — the second is what the ceiling is for, "
                f"so check before raising it.")
        keys = [settings.compute_ssh_key] if settings.compute_ssh_key else []
        label = f"{LABEL}-{int(time.time())}"
        # A public NIC for the object store and package mirrors, and the
        # private VLAN the node reaches the broker on. Without the VLAN the
        # node has no route to the broker and never takes a task.
        interfaces = [
            {"purpose": "public"},
            {"purpose": "vlan", "label": settings.compute_vlan,
             "ipam_address": f"{settings.compute_node_ip}/24"},
        ]
        try:
            machine = self.driver.create(
                label, settings.compute_plan, settings.compute_image,
                settings.compute_region, self._user_data(), keys,
                interfaces=interfaces)
        except ComputeError as exc:
            # NOT retried here. A create that failed may still have made a
            # machine, and retrying is how one becomes three; the next tick
            # reconciles against the provider and will find it.
            return self._alarm(
                "could not create a machine",
                f"{exc}\n\nWork is queued and nothing is rendering it. The "
                f"next tick will look again in case the machine was in fact "
                f"created.")
        store.compute_record_create(machine.id, machine.label)
        return f"created {machine.label} ({machine.id})"

    def _destroy(self, machine: Machine) -> str:
        if not self.enabled:
            return f"would destroy {machine.label} ({self.why_not()})"
        try:
            self.driver.destroy(machine.id)
        except ComputeError as exc:
            return self._alarm(
                "could not destroy a machine",
                f"{machine.label} ({machine.id}) is still running and still "
                f"being charged for: {exc}. It will be retried, but if this "
                f"persists delete it in Cloud Manager.")
        store.compute_record_destroy(machine.id)
        return f"destroyed {machine.label}"

    # -- the loop --------------------------------------------------------
    def run(self, every: float = 30.0) -> None:
        signal.signal(signal.SIGTERM, self._asked_to_stop)
        signal.signal(signal.SIGINT, self._asked_to_stop)
        logger.info("scaler started; %s", self.why_not() or "creation IS live")
        while not self._stop:
            try:
                did = self.tick()
                if did["did"]:
                    logger.info("%s", did["did"])
            except ComputeError as exc:
                logger.warning("tick skipped: %s", exc)
            except Exception as exc:                     # noqa: BLE001
                logger.exception("tick failed")
                self._alarm("the scaler hit an unexpected error",
                            f"{type(exc).__name__}: {exc}\n\nThe loop is "
                            f"still running and will try again.")
            for _ in range(int(every)):
                if self._stop:
                    break
                time.sleep(1)
        logger.info("scaler stopped")

    def _asked_to_stop(self, *_args) -> None:
        # Deliberately does NOT destroy anything on the way out. A restart
        # must not delete a machine that is mid-render; the next tick
        # reconciles and decides again with the queue in front of it.
        self._stop = True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Scaler().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
