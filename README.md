# SimpleClone

Outil de synchronisation unidirectionnelle de dossiers pour Windows.
Surveille un dossier source et réplique automatiquement les changements vers une destination (ex: clé USB).

## Fonctionnalités

| Événement               | Action                                     |
| ----------------------- | ------------------------------------------ |
| Nouveau fichier créé    | Copié vers la destination                  |
| Fichier modifié         | Recopié vers la destination                |
| Fichier supprimé        | Déplacé vers `_Archive/` (avec horodatage) |
| Dossier renommé/déplacé | Reproduit côté destination                 |
| Démarrage               | Synchronisation initiale complète          |
| Clé USB débranchée      | Pause auto + retry + reprise au rebranchement |

### Plus

- **Démarrage avec Windows** (registre HKCU, sans droits admin)
- **Démarrage automatique de la surveillance** à l'ouverture de l'app
- **Zone de notification** (system tray) : la fenêtre se minimise au lieu de se fermer
- **Configuration persistante** : chemins et options sauvegardés dans `%APPDATA%\SimpleClone\config.json`
- **Rotation automatique** du fichier de log (5 Mo × 3 backups)

## Installation

### Prérequis

- Python 3.8 ou supérieur
- pip (gestionnaire de paquets Python)

### Étapes

```bash
# 1. Créer l'environnement virtuel
python -m venv venv

# 2. Activer l'environnement virtuel
# Windows (cmd)
venv\Scripts\activate
# Windows (PowerShell)
.\venv\Scripts\Activate.ps1
# Linux/Mac
source venv/bin/activate

# 3. Installer les dépendances
pip install -r requirements.txt
```

## Utilisation

### Lancer l'application

```bash
# Avec le venv activé
python SimpleClone.py
```

### Mode d'emploi

1. Cliquer sur **Parcourir...** pour sélectionner le dossier **Source** (celui à surveiller)
2. Cliquer sur **Parcourir...** pour sélectionner le dossier **Destination** (ex: clé USB)
3. Cliquer sur **DÉMARRER LA SURVEILLANCE**
4. L'application effectue d'abord une copie initiale, puis surveille les changements en continu

### Arrêter la surveillance

Cliquer sur le bouton rouge **ARRÊTER LA SURVEILLANCE**.

## Démarrage automatique avec Windows

Deux cases à cocher dans l'interface :

- **Démarrer avec Windows** : ajoute une entrée dans
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (pas besoin de droits admin)
- **Lancer la surveillance automatiquement** : démarre la sync dès l'ouverture de l'app

Combinées, ces deux options font tourner SimpleClone en permanence sans intervention. L'app se loge dans la zone de notification (zone à côté de l'horloge) ; double-clic pour rouvrir la fenêtre.

## Comportement clé USB

Si la clé est débranchée pendant la surveillance, l'app **ne crashe pas et ne spamme pas le log**. Elle bascule en pause (indicateur orange, statut "En pause — destination inaccessible") et vérifie toutes les 5 secondes si la clé revient.

Au rebranchement, une resynchronisation complète est lancée automatiquement. Les fichiers qui ont été supprimés de la source pendant la pause sont **déplacés dans `_Archive/`** (jamais supprimés définitivement) pour rester cohérents avec le comportement habituel de l'app.

Vous pouvez donc débrancher la clé sans précaution particulière (mais toujours via "Éjecter le périphérique" pour éviter une corruption FS).

## Créer un exécutable Windows (.exe)

### Option 1 : automatique via GitHub Actions (recommandé)

Un workflow `.github/workflows/build-windows.yml` est inclus. Il déclenche un build Windows à chaque tag `v*.*.*` :

```bash
git tag v1.2.0
git push --tags
```

L'exécutable apparaît ensuite dans l'onglet **Releases** du dépôt GitHub. Pas besoin d'avoir Windows en local.

### Option 2 : manuel sur Windows

```bash
# 1. Activer le venv
venv\Scripts\activate

# 2. Installer PyInstaller
pip install pyinstaller

# 3. Créer l'exécutable
pyinstaller --onefile --windowed --name "SimpleClone" SimpleClone.py
```

L'exécutable sera créé dans le dossier `dist/SimpleClone.exe`.

### Avec une icône personnalisée

```bash
pyinstaller --onefile --windowed --name "SimpleClone" --icon=monicon.ico SimpleClone.py
```

## Fichiers générés

```
log/
├── activity/
│   ├── activity.log                  ← fichier courant (jour J)
│   ├── activity.log.2026-05-03       ← rotation à minuit
│   ├── activity.log.2026-05-02
│   └── ...                           ← conservés 6 ans (≈2190 fichiers)
└── errors/
    ├── SimpleClone_Errors.log        ← rotation par taille (5 Mo × 3)
    ├── SimpleClone_Errors.log.1
    └── ...
```

Le dossier `log/` est créé **à côté de l'exécutable** par défaut. Si l'exe est installé dans un emplacement non inscriptible (typiquement `Program Files`), il bascule automatiquement sur `%APPDATA%\SimpleClone\log\`. Le chemin effectif est affiché dans le journal de l'application au démarrage.

À côté de la destination on trouve aussi :

| Fichier     | Description                                                  |
| ----------- | ------------------------------------------------------------ |
| `_Archive/` | Fichiers et dossiers supprimés de la source (avec horodatage) |

## Audit (preuve de copie)

Le fichier `log/activity/activity.log` enregistre **chaque opération réussie** au format JSON-lines (une ligne par événement) :

```json
{"ts": "2026-05-04T11:42:04", "op": "copy_initial", "src": "C:/data/autoclave_001.csv", "dest": "E:/backup/autoclave_001.csv", "size": 18432}
{"ts": "2026-05-04T11:42:09", "op": "copy", "src": "C:/data/cycle_42.txt", "dest": "E:/backup/cycle_42.txt", "size": 24500}
{"ts": "2026-05-04T11:43:11", "op": "archive", "src": "C:/data/old.csv", "dest": "E:/backup/_Archive/old_20260504_114311.csv", "size": 12000}
```

**Opérations tracées :**

| `op`              | Quand                                                                |
| ----------------- | -------------------------------------------------------------------- |
| `copy`            | Fichier nouveau ou modifié détecté en surveillance                   |
| `copy_initial`    | Fichier copié pendant la synchronisation initiale                    |
| `archive`         | Fichier supprimé de la source → déplacé dans `_Archive/`            |
| `archive_dir`     | Dossier supprimé de la source                                        |
| `archive_orphan`  | Fichier orphelin déplacé après une reprise de pause USB              |
| `rename`          | Fichier ou dossier renommé/déplacé                                   |

**Rétention :** rotation quotidienne à minuit, **conservation 6 ans** (politique adaptée à un cycle d'archivage légal de 5 ans + marge). Les fichiers plus anciens sont supprimés automatiquement.

**Exploitation :** le format JSON-lines permet d'extraire facilement les preuves :

```bash
# Toutes les copies d'un fichier précis
grep autoclave_001 log/activity/activity.log*

# Avec jq, toutes les copies d'une journée
cat log/activity/activity.log.2026-05-03 | jq 'select(.op | startswith("copy"))'
```

## Gestion des erreurs

Le script ne crash jamais. Si un fichier ne peut pas être copié (verrouillé, permissions, chemin trop long), il est :

- Enregistré dans `log/errors/SimpleClone_Errors.log`
- Ignoré, le script continue avec le fichier suivant

Le log d'erreurs utilise une rotation **par taille** (5 Mo × 3 fichiers maximum), distincte du log d'activité — les erreurs sont du debug technique, pas un audit légal.

## Structure du projet

```
simpleClone/
├── SimpleClone.py        # Script principal
├── requirements.txt      # Dépendances runtime
├── requirements-dev.txt  # Dépendances de dev (pytest)
├── tests/                # Suite pytest
├── README.md             # Documentation
└── venv/                 # Environnement virtuel (après installation)
```

## Tests

Le projet dispose d'une suite pytest qui couvre la configuration, le système de logs, et la logique de synchronisation (copie, archivage, détection de débranchement, non-récursion sur `_Archive/`).

```bash
# Une seule fois : installer les dépendances de dev
pip install -r requirements-dev.txt

# Lancer la suite
pytest -v
```

Les tests stub `tkinter` et `watchdog` au niveau import : aucun environnement graphique n'est requis. Les loggers sont isolés par test (chaque test reçoit son propre dossier `log/` temporaire).

La CI GitHub (`.github/workflows/test.yml`) exécute automatiquement les tests à chaque push sur `main` et chaque pull request.

## Dépendances

**Runtime** :

- **watchdog** : surveillance du système de fichiers
- **pystray** + **Pillow** : zone de notification (system tray)

**Développement** :

- **pytest** : suite de tests
