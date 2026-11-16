from pathlib import Path


class DeployManager:
    def __init__(self, git, tests, supervisor, storage):
        self.git, self.tests, self.supervisor, self.storage = (
            git,
            tests,
            supervisor,
            storage,
        )

    async def prepare(self):
        await self.git.clean()
        result = await self.tests.execute()
        if not result["passed"]:
            raise RuntimeError("Deployment checks failed: " + str(result))
        return {
            "fingerprint": await self.git.fingerprint(),
            "commit": (await self.git.identity())["head"],
            "tests": result,
        }

    async def release(self, commit):
        path = self.git.root / "data" / "dev_agent" / "releases" / commit
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.git.git("worktree", "add", "--detach", str(path), commit)
        elif await self.git.git("-C", str(path), "rev-parse", "HEAD") != commit:
            raise ValueError("Release commit mismatch")
        if await self.git.git("-C", str(path), "status", "--porcelain"):
            raise ValueError("Release directory was modified")
        return path

    async def deploy(self, prepared):
        await self.git.clean()
        if prepared["fingerprint"] != await self.git.fingerprint():
            raise ValueError("Files changed since tests; prepare deployment again")
        candidate = await self.release(prepared["commit"])
        previous = self.storage.get("verified_release")
        if self.supervisor.status()["running"] and not previous:
            raise ValueError(
                "Running OPS version has no recovery point; start a baseline with --start-ops first"
            )
        self.storage.put(
            "deployment",
            {"state": "starting", "candidate": str(candidate), "previous": previous},
        )
        try:
            await self.supervisor.stop()
            status = await self.supervisor.start(candidate)
        except (RuntimeError, OSError) as exc:
            await self.supervisor.stop()
            if previous:
                await self.supervisor.start(previous)
            self.storage.put(
                "deployment",
                {"state": "failed", "error": str(exc), "restored": bool(previous)},
            )
            raise
        # Partial startup checks are recorded honestly; owner can confirm operational health.
        self.storage.put("previous_release", previous)
        self.storage.put("verified_release", str(candidate))
        self.storage.put(
            "deployment", {"state": "partial_health", "commit": prepared["commit"]}
        )
        return status

    async def rollback(self):
        previous = self.storage.get("previous_release")
        if not previous or not Path(previous).is_dir():
            raise ValueError("No previous release available")
        if await self.git.git("-C", previous, "status", "--porcelain"):
            raise ValueError("Previous release was modified; refusing rollback")
        current = self.storage.get("verified_release")
        await self.supervisor.stop()
        try:
            status = await self.supervisor.start(previous)
        except (RuntimeError, OSError):
            await self.supervisor.stop()
            if current:
                await self.supervisor.start(current)
            raise
        self.storage.put("verified_release", previous)
        self.storage.put("previous_release", current)
        self.storage.put("deployment", {"state": "rolled_back", "release": previous})
        return status
