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

from .. import storage, store
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

        acted = ""
        if wanted and not live:
            acted = self._create()
        elif not wanted and live:
            acted = self._destroy(live)

        return {**shadow, "machine": live.id if live else None, "did": acted}

    def _reconcile(self) -> Machine | None:
        """What the PROVIDER says exists, not what we remember."""
        if self.driver is None:
            return None
        try:
            ours = self.driver.find_ours()
        except ComputeError as exc:
            logger.warning("could not list machines: %s", exc)
            # Unknown is not "none". Returning None here would look like
            # "no machine exists" and create a second one.
            raise
        if len(ours) > 1:
            logger.error("%d machines named %s exist; expected at most one. "
                         "Not touching any of them.", len(ours), LABEL)
            raise ComputeError("more than one compute machine exists")
        return ours[0] if ours else None

    def _create(self) -> str:
        if not self.enabled:
            return f"would create ({self.why_not()})"
        made = store.compute_creates_this_hour()
        if made >= settings.compute_max_creates_per_hour:
            logger.error("refusing to create: %d already this hour, the "
                         "ceiling is %d", made, settings.compute_max_creates_per_hour)
            return "refused, at the hourly ceiling"
        keys = [settings.compute_ssh_key] if settings.compute_ssh_key else []
        label = f"{LABEL}-{int(time.time())}"
        try:
            machine = self.driver.create(
                label, settings.compute_plan, settings.compute_image,
                settings.compute_region, self._user_data(), keys)
        except ComputeError as exc:
            logger.error("create failed: %s", exc)
            return f"create failed: {exc}"
        store.compute_record_create(machine.id, machine.label)
        return f"created {machine.label} ({machine.id})"

    def _destroy(self, machine: Machine) -> str:
        if not self.enabled:
            return f"would destroy {machine.label} ({self.why_not()})"
        try:
            self.driver.destroy(machine.id)
        except ComputeError as exc:
            logger.error("destroy failed: %s", exc)
            return f"destroy failed: {exc}"
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
            except Exception:                            # noqa: BLE001
                logger.exception("tick failed")
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
