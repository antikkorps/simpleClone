"""Tests pour ConfigManager : load/save round-trip, robustesse aux corruptions."""
import json


def test_load_returns_defaults_when_file_missing(sc, tmp_path, monkeypatch):
    """Une config absente ne doit pas crasher : on retombe sur les defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm = sc.ConfigManager()
    cm.load()

    assert cm.get("source_path") == ""
    assert cm.get("dest_path") == ""
    assert cm.get("polling_interval") == sc.DEFAULT_POLLING_INTERVAL
    assert cm.get("autostart_windows") is False


def test_save_then_load_round_trip(sc, tmp_path, monkeypatch):
    """Une config sauvegardée doit être relue à l'identique."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm1 = sc.ConfigManager()
    cm1.set("source_path", "/data/source")
    cm1.set("dest_path", "/media/usb")
    cm1.set("autostart_windows", True)
    cm1.set("autostart_surveillance", True)

    cm2 = sc.ConfigManager()
    cm2.load()

    assert cm2.get("source_path") == "/data/source"
    assert cm2.get("dest_path") == "/media/usb"
    assert cm2.get("autostart_windows") is True
    assert cm2.get("autostart_surveillance") is True


def test_corrupted_json_falls_back_to_defaults(sc, tmp_path, monkeypatch):
    """Un fichier JSON cassé ne doit pas faire crasher l'app au démarrage."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm = sc.ConfigManager()
    cm.config_dir.mkdir(parents=True, exist_ok=True)
    cm.config_file.write_text("{ this is not valid json")

    # Doit passer sans exception et retomber sur les valeurs par défaut
    cm.load()
    assert cm.get("source_path") == ""
    assert cm.get("polling_interval") == sc.DEFAULT_POLLING_INTERVAL


def test_unknown_keys_are_ignored(sc, tmp_path, monkeypatch):
    """Une config avec des clés inconnues (vieille version, intrus) ne pollue pas le state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm = sc.ConfigManager()
    cm.config_dir.mkdir(parents=True, exist_ok=True)
    cm.config_file.write_text(json.dumps({
        "source_path": "/legit",
        "unknown_legacy_key": "ignore_me",
        "another_garbage": 42,
    }))

    cm.load()
    assert cm.get("source_path") == "/legit"
    assert "unknown_legacy_key" not in cm.data
    assert "another_garbage" not in cm.data


def test_set_persists_immediately(sc, tmp_path, monkeypatch):
    """Set doit déclencher une sauvegarde sans avoir à appeler save() à la main."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm = sc.ConfigManager()
    cm.set("polling_interval", 5)

    # Lit le fichier directement, sans passer par load()
    raw = json.loads(cm.config_file.read_text())
    assert raw["polling_interval"] == 5


def test_set_ignores_unknown_keys(sc, tmp_path, monkeypatch):
    """set() sur une clé non déclarée dans DEFAULTS doit être un no-op silencieux."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)

    cm = sc.ConfigManager()
    cm.set("not_a_real_setting", "value")

    assert "not_a_real_setting" not in cm.data
