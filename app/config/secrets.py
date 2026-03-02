import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class CredentialProfile:
    api_key_id: str = ""
    private_key_path: str = ""


class SecretStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict:
        if not self.path.exists():
            return {"paper": asdict(CredentialProfile()), "production": asdict(CredentialProfile())}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def load(self, env: str) -> CredentialProfile:
        payload = self.load_all()
        return CredentialProfile(**payload.get(env, {}))

    def save(self, env: str, profile: CredentialProfile) -> None:
        payload = self.load_all()
        payload[env] = asdict(profile)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
