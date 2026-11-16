from syncalist.crypto import Secrets


def test_round_trip_and_key_persists(tmp_path):
    key = tmp_path / "secret.key"
    a = Secrets(key)
    enc = a.encrypt("token-123")
    assert enc != "token-123"
    b = Secrets(key)  # reloads the same key from disk
    assert b.decrypt(enc) == "token-123"
    assert a.session_secret == b.session_secret
    assert oct(key.stat().st_mode & 0o777) == "0o600"


def test_bad_ciphertext_returns_none(tmp_path):
    s = Secrets(tmp_path / "k")
    assert s.decrypt("not-valid") is None
    assert s.decrypt(None) is None
