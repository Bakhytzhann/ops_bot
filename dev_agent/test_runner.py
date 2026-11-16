import os

from .process import run


class TestRunner:
    __test__ = False

    def __init__(self, python, root):
        self.python, self.root = python, root

    async def execute(self):
        results = []
        commands = [
            [
                "-m",
                "compileall",
                "-q",
                "dev_agent",
                "application",
                "bot",
                "services",
                "config",
                "main.py",
            ],
            ["-m", "ruff", "check", "."],
            ["-m", "pytest", "-q"],
        ]
        for command in commands:
            code, output = await run(
                [self.python, *command],
                self.root,
                900,
                env={**os.environ, "DEBUG": "false"},
            )
            results.append({"command": command, "code": code, "output": output[-6000:]})
            if code:
                break
        return {"passed": all(r["code"] == 0 for r in results), "results": results}
