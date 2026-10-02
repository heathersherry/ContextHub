"""Run explicit offline checks in a clean environment and new evidence directory."""

import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=False)
env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
env.update(
    PYTHONPATH=os.pathsep.join(
        map(
            str,
            [
                root / "src",
                root / "sdk/src",
                root,
                root / "tests",
                Path(__file__).parent,
            ],
        )
    ),
    PYTHONDONTWRITEBYTECODE="1",
    PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
)
cmd = [
    sys.executable,
    "-m",
    "pytest",
    "-p",
    "offline_guard",
    "-p",
    "pytest_asyncio.plugin",
    "-q",
    *sys.argv[2:],
    "--basetemp=" + str(out / "tmp"),
    "--junitxml=" + str(out / "results.xml"),
]
(out / "command.json").write_text(
    json.dumps(dict(argv=cmd, cwd=str(root), environment=env), indent=2)
)
with (out / "pytest.log").open("w") as log:
    result = subprocess.run(
        cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT
    )
(out / "result.json").write_text(json.dumps({"exit_code": result.returncode}))
print((out / "pytest.log").read_text()[-9000:])
sys.exit(result.returncode)
