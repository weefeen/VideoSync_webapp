"""Compare two measures.data files for the same recording.

Two alignment modes should place the same measure at nearly the same
moment. Large disagreement means at least one of them is wrong, which a
successful exit code alone would never reveal.

Usage:
    python tools/compare_alignments.py <a.measures.data> <b.measures.data>
"""

import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.package import _read_measures   # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a_path, b_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    a = dict(_read_measures(a_path))
    b = dict(_read_measures(b_path))

    shared = sorted(set(a) & set(b))
    print(f"A: {len(a):>5} measures  {a_path.parent.parent.parent.name}")
    print(f"B: {len(b):>5} measures  {b_path.parent.parent.parent.name}")
    print(f"shared: {len(shared)}  only-A: {len(set(a) - set(b))}  "
          f"only-B: {len(set(b) - set(a))}")
    if not shared:
        print("\nNo measures in common — the two alignments disagree entirely.")
        return 1

    deltas = [a[m] - b[m] for m in shared]
    absd = sorted(abs(d) for d in deltas)

    def pct(p: float) -> float:
        return absd[min(len(absd) - 1, int(len(absd) * p))]

    print(f"\ndelta A-B (seconds):")
    print(f"  mean   {statistics.fmean(deltas):+.3f}")
    print(f"  median {statistics.median(deltas):+.3f}")
    print(f"  |d| p50 {pct(0.50):.3f}   p90 {pct(0.90):.3f}   "
          f"p99 {pct(0.99):.3f}   max {absd[-1]:.3f}")

    for tol in (0.05, 0.10, 0.25, 0.50, 1.00):
        n = sum(1 for d in absd if d <= tol)
        print(f"  within {tol:>4.2f}s: {n:>5}/{len(absd)}  ({100 * n / len(absd):5.1f}%)")

    worst = sorted(shared, key=lambda m: -abs(a[m] - b[m]))[:5]
    print("\nlargest disagreements:")
    for m in worst:
        print(f"  measure {m:>5}   A={a[m]:8.3f}s  B={b[m]:8.3f}s  "
              f"delta={a[m] - b[m]:+.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
