from app.config.secrets import CredentialProfile, SecretStore


def test_secret_store_profiles(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.save("paper", CredentialProfile(api_key_id="k1", private_key_path="a.key"))
    store.save("production", CredentialProfile(api_key_id="k2", private_key_path="b.key"))
    assert store.load("paper").api_key_id == "k1"
    assert store.load("production").private_key_path == "b.key"
