"""
Tests d'intégration sur les imports du module UI.

Ces tests ne couvrent PAS le rendu Tkinter (impossible sans display), mais
attrapent les bugs où ui.py oublie d'importer un symbole nécessaire au
runtime. C'est précisément ce genre de bug qui se cache derrière une callback
tkinter (où NameError est avalé silencieusement) et ne sort pas en dev.
"""


def test_ui_module_imports_underscored_helpers_from_core(sc):
    """
    Régression v0.11.0 : `from simpleclone_core import *` filtre les noms
    underscored par défaut en Python. Si on oublie de les ré-importer
    explicitement, les fonctions UI qui les utilisent crashent en NameError —
    avalé par tk dans un callback, ça donne un dialog vide à l'utilisateur.
    """
    import simpleclone_ui

    # Helpers utilisés directement par _show_help_dialog et _create_diagnostic_file
    required = [
        "_resolve_log_dir",
        "_format_uptime",
        "_find_last_activity_event",
        "_state_to_french",
    ]
    missing = [name for name in required if not hasattr(simpleclone_ui, name)]
    assert not missing, (
        f"simpleclone_ui n'a pas accès à : {missing}. "
        f"`from simpleclone_core import *` skip les underscored — "
        f"il faut les importer explicitement."
    )


def test_ui_module_exposes_public_core_symbols(sc):
    """
    L'autre moitié du contrat : les symboles publics du core doivent rester
    accessibles depuis ui.py via le star import. Si quelqu'un ajoute un
    `__all__` restrictif au core sans s'en rendre compte, ce test détecte
    la rupture.
    """
    import simpleclone_ui

    required = [
        "ConfigManager", "SyncHandler",
        "log_activity", "initial_sync",
        "build_diagnostic_text", "write_diagnostic_file", "reveal_in_file_manager",
        "set_windows_autostart", "is_windows_autostart_enabled",
        "STATE_RUNNING", "STATE_STOPPED", "STATE_PAUSED_USB",
        "HEARTBEAT_INTERVAL_S", "MAX_LOG_LINES_UI", "DEFAULT_POLLING_INTERVAL",
        "ERRORS_LOG_FILE_NAME",
    ]
    missing = [name for name in required if not hasattr(simpleclone_ui, name)]
    assert not missing, f"Symboles publics manquants dans simpleclone_ui : {missing}"
