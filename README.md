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

| Fichier                  | Description                                                               |
| ------------------------ | ------------------------------------------------------------------------- |
| `SimpleClone_Errors.log` | Journal des erreurs (fichiers non copiés)                                 |
| `_Archive/`              | Dossier dans la destination contenant les fichiers supprimés de la source |

## Gestion des erreurs

Le script ne crash jamais. Si un fichier ne peut pas être copié (verrouillé, permissions, chemin trop long), il est :

- Enregistré dans `SimpleClone_Errors.log`
- Ignoré, le script continue avec le fichier suivant

## Structure du projet

```
simpleClone/
├── SimpleClone.py      # Script principal
├── requirements.txt    # Dépendances Python
├── README.md          # Documentation
└── venv/              # Environnement virtuel (après installation)
```

## Dépendances

- **watchdog** : Bibliothèque de surveillance du système de fichiers
