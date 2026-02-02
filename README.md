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

## IMPORTANT - Clé USB

**Arrêtez TOUJOURS la surveillance AVANT de débrancher la clé USB !**

Si la clé est débranchée pendant que la surveillance tourne, des erreurs "Disque non prêt" seront générées en boucle.

## Créer un exécutable Windows (.exe)

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
