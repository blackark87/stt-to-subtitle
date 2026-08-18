# WhisperJAV golden fixtures

## `resolved_pass_params.json`

The parameter set upstream's configuration layer resolves for our fixed
two-pass recipe. `presets.py` transcribes these values as literals, and
`tests/test_whisperjav_vendor.py::PresetSnapshotTests` fails if the two drift.

Regenerate inside a container that still has the upstream `whisperjav` package
installed:

```bash
docker exec -i stt-backend sh -c 'cat > /tmp/dump.py' \
    < tests/golden/whisperjav/dump_resolved_pass_params.py
docker exec stt-backend /opt/venvs/whisperjav/bin/python /tmp/dump.py \
    | grep -o '@@@JSON@@@.*' | sed 's/@@@JSON@@@//' | python3 -m json.tool \
    > tests/golden/whisperjav/resolved_pass_params.json
```

## Payload regression

`compare_payloads.py` diffs two worker payloads produced from the same audio.
It needs a GPU, so it is a manual check rather than part of the unit suite.

```bash
# before switching images, with the old code:
docker exec stt-backend /opt/venvs/whisperjav/bin/python - <<'PY' > baseline.json
from pathlib import Path; import json
from stt_to_subtitle.whisperjav_worker import run_whisperjav
print(json.dumps(run_whisperjav(Path("/tmp/sample.wav"), {}), ensure_ascii=False))
PY
# after rebuilding:
python3 tests/golden/whisperjav/compare_payloads.py baseline.json candidate.json
```

Cue text must match exactly; timestamps are compared with a 50 ms tolerance.
Recorded payloads are not committed — they contain transcribed dialogue.
