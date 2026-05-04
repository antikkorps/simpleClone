# -*- coding: utf-8 -*-
"""
SimpleClone - Point d'entrée

Ce fichier est volontairement minimal : il parse les arguments CLI, crée la
racine Tkinter et instancie SimpleCloneApp. La logique métier vit dans
simpleclone_core.py, l'UI dans simpleclone_ui.py.

Conservé sous le nom SimpleClone.py (avec majuscule) car :
- c'est le nom historique du projet, exposé dans la doc utilisateur
- PyInstaller utilise ce fichier comme entry point pour produire SimpleClone.exe
- la commande d'autostart Windows enregistrée dans le registre référence ce nom
"""

import argparse
import tkinter as tk

from simpleclone_ui import SimpleCloneApp


def _parse_args():
    """Parse les arguments CLI. --minimized est passé par l'autostart Windows."""
    parser = argparse.ArgumentParser(
        description="SimpleClone - Synchronisation unidirectionnelle de dossiers"
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Démarre l'application dans la zone de notification (pas de fenêtre)."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    root = tk.Tk()
    app = SimpleCloneApp(root, args=args)
    root.mainloop()
