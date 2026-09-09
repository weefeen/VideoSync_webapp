"""Where one job's files live, and which of them are worth keeping.

The layout is VideoScoreSync's, deliberately and exactly — `input/`,
`sync_data/`, `output/`, `static_pages/`, with the same file names inside.
Two reasons. The engine's workers already describe a job this way, so when
the stages move into their own processes there is nothing to translate. And
one directory becomes the whole job, which is what makes archiving and
deleting a single operation instead of a list of places to remember.

The webapp did not do this. The upload lived in a shared `uploads/` folder
while everything else lived under the job's own id, so a job's files were in
two places — which is why `pipeline.cleanup()` was never called from
anywhere: it only knew about one of them, so calling it would have left the
larger half behind. Nothing has ever been deleted.

    <work_dir>/<job_id>/
        input/          <job_id>.<ext>      the upload, as it arrived
                        job_params.json     score, style, mode  (never the email)
        sync_data/      audio.wav           22050 Hz mono, what chroma reads
                        chroma.npy
                        measures.data
                        verdict.json        what the recogniser decided
        output/         <base>_PROCESSED.<ext>   the video someone waited for
        static_pages/   static_info.json

`verdict.json` is the one addition: VideoScoreSync has no recognition stage,
so it has no equivalent.

Three tiers hold these files over a job's life:

    performance   this directory, on the machine doing the work. One job at
                  a time. On the compute instance it dies with the instance,
                  so a render finishing and the result being safe are two
                  different events.
    hot           what a visitor can download, for RETENTION_HOT_HOURS.
    archive       the same objects afterwards, in Glacier.

`keep()` is what crosses into the hot tier and then the archive: the video,
the recording it was made from, and the small artefacts that are expensive
to recompute. With the chroma and the alignment kept, re-rendering a job in
different colours is a two-minute encode rather than the whole job again.

The upload is kept on purpose. Recordings made at home — whatever
instrument, whatever microphone, whatever room — are the one thing a studio
library cannot supply and the thing recognition most needs to be robust
against. That is a commitment rather than a convenience: they are
recordings of identifiable people, held indefinitely, so the page says
plainly that we hold them, that nobody is shown them, and that they are
deleted on request. If that promise changes, this function is where it
changes, and the note on the page has to move with it.

`discard()` is the extracted audio and the encoder's leavings. The audio
goes because it is a lossy re-encode of a file we are already keeping;
holding both is paying twice for one recording.
"""

from __future__ import annotations

import dataclasses
import pathlib

from .settings import settings

# Folder names, matching VideoScoreSync's config.py:350-360 exactly.
INPUT = "input"
SYNC_DATA = "sync_data"
OUTPUT = "output"
STATIC_PAGES = "static_pages"
PROCESSED_SUFFIX = "_PROCESSED"

AUDIO_FOR_SYNC = f"{SYNC_DATA}/audio.wav"
CHROMA = f"{SYNC_DATA}/chroma.npy"
MEASURES = f"{SYNC_DATA}/measures.data"
VERDICT = f"{SYNC_DATA}/verdict.json"          # ours; the engine has no recogniser
JOB_PARAMS = f"{INPUT}/job_params.json"
STATIC_INFO = f"{STATIC_PAGES}/static_info.json"


@dataclasses.dataclass(frozen=True)
class JobPaths:
    """Every path for one job, derived from its id and its upload's suffix."""

    job_id: str
    suffix: str = ".mp4"

    @property
    def root(self) -> pathlib.Path:
        return settings.work_dir / self.job_id

    def _p(self, relative: str) -> pathlib.Path:
        return self.root / relative

    # -- the four folders ------------------------------------------------
    @property
    def input_dir(self) -> pathlib.Path:
        return self._p(INPUT)

    @property
    def sync_dir(self) -> pathlib.Path:
        return self._p(SYNC_DATA)

    @property
    def output_dir(self) -> pathlib.Path:
        return self._p(OUTPUT)

    @property
    def static_dir(self) -> pathlib.Path:
        return self._p(STATIC_PAGES)

    # -- the files -------------------------------------------------------
    @property
    def upload(self) -> pathlib.Path:
        """The recording as it arrived, named by job id rather than by what
        the visitor called it: their file name is untrusted text, and it has
        no business becoming a path on our disk."""
        return self.input_dir / f"{self.job_id}{self.suffix}"

    @property
    def job_params(self) -> pathlib.Path:
        return self._p(JOB_PARAMS)

    @property
    def audio(self) -> pathlib.Path:
        return self._p(AUDIO_FOR_SYNC)

    @property
    def chroma(self) -> pathlib.Path:
        return self._p(CHROMA)

    @property
    def measures(self) -> pathlib.Path:
        return self._p(MEASURES)

    @property
    def verdict(self) -> pathlib.Path:
        return self._p(VERDICT)

    @property
    def static_info(self) -> pathlib.Path:
        return self._p(STATIC_INFO)

    def result(self, stem: str) -> pathlib.Path:
        """The finished video. `stem` is the score's name, from our own
        library — never anything the visitor typed."""
        return self.output_dir / f"{stem}{PROCESSED_SUFFIX}{self.suffix}"

    def existing_result(self) -> pathlib.Path | None:
        """Whatever was rendered, without needing to know the score's name."""
        if not self.output_dir.is_dir():
            return None
        for found in sorted(self.output_dir.glob(f"*{PROCESSED_SUFFIX}.*")):
            return found
        return None

    # -- lifecycle -------------------------------------------------------
    def make(self) -> "JobPaths":
        for folder in (self.input_dir, self.sync_dir,
                       self.output_dir, self.static_dir):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def keep(self) -> list[pathlib.Path]:
        """Files that cross to the hot tier and then to the archive.

        The video, the recording it was made from, and the small artefacts
        that are expensive to recompute: with the chroma and the alignment,
        a re-render is a two-minute encode rather than the whole job again.

        The upload is kept deliberately. Home recordings — whatever
        instrument, whatever microphone, whatever room — are the one thing a
        studio library cannot supply and the thing the recogniser most needs
        to be robust against. Keeping them is a commitment, though, not a
        convenience: they are recordings of identifiable people, so the
        privacy note says plainly that we hold them, that nobody sees them,
        and that they are deleted on request.
        """
        wanted = [self.upload, self.chroma, self.measures, self.verdict,
                  self.job_params, self.static_info]
        result = self.existing_result()
        if result:
            wanted.insert(0, result)
        return [p for p in wanted if p.is_file()]

    def discard(self) -> list[pathlib.Path]:
        """Files deleted once the result is safely stored.

        The extracted audio goes: it is a lossy re-encode of the upload we
        are keeping, so holding both is paying twice for the same recording.
        Everything else here is an intermediate the encode left behind.
        """
        rubbish = [self.audio]
        if self.output_dir.is_dir():
            rubbish += [p for p in self.output_dir.iterdir()
                        if p.is_file() and PROCESSED_SUFFIX not in p.name]
        return [p for p in rubbish if p.is_file()]

    def bytes_kept(self) -> int:
        return sum(p.stat().st_size for p in self.keep())


def for_job(job_id: str, suffix: str = ".mp4") -> JobPaths:
    return JobPaths(job_id=job_id, suffix=suffix or ".mp4")
