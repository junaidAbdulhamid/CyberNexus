#!/usr/bin/env bash
# Fetch a public IDS dataset for training/evaluation on real traffic.
#
# Both datasets require accepting the provider's terms, and the download links
# change periodically, so this script does not scrape them: it points you at the
# canonical source, then verifies and loads whatever you place in ./data.
#
#   ./model/get_dataset.sh unsw    # UNSW-NB15  (Moustafa & Slay, 2015)
#   ./model/get_dataset.sh cic     # CIC-IDS-2017 (Sharafaldin et al., 2018)
set -euo pipefail

KIND="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"

case "$KIND" in
  unsw)
    TARGET="$DATA_DIR/unsw"
    cat <<'TXT'
UNSW-NB15
  Source:  https://research.unsw.edu.au/projects/unsw-nb15-dataset
  Files:   either the four part files UNSW-NB15_1.csv ... UNSW-NB15_4.csv
           (plus NUSW-NB15_features.csv), or the prepared split
           UNSW_NB15_training-set.csv / UNSW_NB15_testing-set.csv
  Size:    ~600 MB for the full part files
TXT
    ;;
  cic)
    TARGET="$DATA_DIR/cic"
    cat <<'TXT'
CIC-IDS-2017
  Source:  https://www.unb.ca/cic/datasets/ids-2017.html
  Files:   the MachineLearningCSV bundle (8 files, *.pcap_ISCX.csv)
  Size:    ~500 MB extracted
TXT
    ;;
  *)
    echo "usage: $0 {unsw|cic}" >&2
    exit 2
    ;;
esac

mkdir -p "$TARGET"
echo
echo "Put the CSV files in: $TARGET"
COUNT=$(find "$TARGET" -maxdepth 1 -name '*.csv' | wc -l | tr -d ' ')
if [[ "$COUNT" -eq 0 ]]; then
  echo "No CSVs found there yet."
  exit 1
fi

echo "Found $COUNT CSV file(s).  Training on them:"
echo
cd "$HERE"
export PYTHONPATH="$HERE"
PYTHON="${PYTHON:-$( [[ -x ../.venv/bin/python ]] && echo ../.venv/bin/python || echo python3 )}"
exec "$PYTHON" -m model.train --dataset "$KIND" --data "$TARGET" --out "artifacts/model-$KIND"
