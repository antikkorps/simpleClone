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

Le build produit un **dossier `SimpleClone/`** (mode `--onedir`) packagé en `SimpleClone-<version>-windows.zip` et publié dans l'onglet **Releases** avec son empreinte SHA256.

> **Pourquoi `--onedir` et pas `--onefile` ?** Les exécutables `--onefile` PyInstaller embarquent un mini-extracteur qui décompresse l'app dans un dossier temporaire à chaque lancement — comportement très similaire à des techniques de packing utilisées par les malwares, ce qui déclenche fréquemment **Windows Defender** (faux positifs, fichier supprimé silencieusement de la clé USB, etc.). Le mode `--onedir` produit un dossier transparent : un `.exe` standard accompagné de ses DLLs. Démarrage plus rapide, beaucoup moins de faux positifs antivirus.

### Option 2 : manuel sur Windows

```bash
# 1. Activer le venv
venv\Scripts\activate

# 2. Installer PyInstaller
pip install pyinstaller

# 3. Créer l'exécutable (dossier complet)
pyinstaller --onedir --windowed --name "SimpleClone" SimpleClone.py
```

Le résultat est dans `dist/SimpleClone/` — il faut **distribuer le dossier entier**, pas seulement `SimpleClone.exe`.

### Avec une icône personnalisée

```bash
pyinstaller --onedir --windowed --name "SimpleClone" --icon=monicon.ico SimpleClone.py
```

## Le `.exe` est bloqué par Windows ?

Les binaires Python empaquetés (PyInstaller) **ne sont pas signés** et déclenchent régulièrement des faux positifs avec Windows Defender et SmartScreen. Symptômes typiques :

- Au téléchargement : "Ce fichier n'est pas couramment téléchargé" (SmartScreen).
- À la copie sur clé USB : "Vous avez besoin des droits administrateur pour copier ce fichier".
- Pire : **l'exe disparaît tout seul** d'une clé USB quelques minutes après la copie (mis en quarantaine par Defender).

### Vérifier d'abord que le fichier n'a pas été altéré

Le hash SHA256 publié sur la Release sert exactement à ça :

```powershell
# Dans le dossier du téléchargement
Get-FileHash .\SimpleClone-0.11.1-windows.zip -Algorithm SHA256
```

La valeur affichée doit être identique à celle du fichier `.sha256` publié à côté du zip. Si elle diffère, **ne pas utiliser le fichier** : il a été modifié (ou un antivirus en a tronqué une partie).

### Débloquer le fichier (Mark of the Web)

Tout fichier téléchargé depuis Internet reçoit une marque "zone Internet" qui durcit les restrictions. Sur le **zip**, avant extraction :

1. Clic droit sur `SimpleClone-x.y.z-windows.zip` → **Propriétés**
2. En bas de l'onglet **Général**, cocher **"Débloquer"** → **Appliquer**
3. Extraire ensuite (la marque ne se propage pas aux fichiers extraits)

### Récupérer un fichier mis en quarantaine

Si l'exe a disparu après copie sur clé USB :

1. Ouvrir **Sécurité Windows** → **Protection contre les virus et menaces**
2. Cliquer sur **Historique de protection**
3. Repérer la ligne `SimpleClone.exe` → **Actions** → **Restaurer**

Pour éviter que ça recommence, ajouter une exclusion (voir ci-dessous).

### Ajouter une exclusion Windows Defender

Si vous savez ce que vous faites et avez vérifié le hash SHA256 :

1. **Sécurité Windows** → **Protection contre les virus et menaces** → **Gérer les paramètres** (sous "Paramètres de protection contre les virus et menaces")
2. Tout en bas : **Ajouter ou supprimer des exclusions**
3. Ajouter une exclusion de type **Dossier** pointant vers le dossier d'installation de SimpleClone (ex : `C:\Program Files\SimpleClone\`)
4. Ajouter aussi le lecteur de la clé USB cible si Defender bloque la copie

### Si rien ne fonctionne

Signaler le faux positif à Microsoft : https://www.microsoft.com/en-us/wdsi/filesubmission — cocher "I believe this file should not be detected as malware". Une fois traité (généralement quelques jours), la signature problématique est retirée pour tous les utilisateurs.

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
| `app_start`       | Démarrage de l'application                                           |
| `heartbeat`       | Toutes les 15 minutes — preuve que l'app tourne (avec `state`, `uptime_s`) |
| `app_stop`        | Fermeture propre de l'application (avec `uptime_s`)                  |
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

# Dernier heartbeat (pour vérifier que l'app tourne)
grep heartbeat log/activity/activity.log | tail -1
```

## Mise en production

Cette section s'adresse à l'administrateur qui déploie SimpleClone sur un poste client en exploitation continue. Les recommandations ci-dessous sont issues du retour d'expérience terrain et conditionnent la valeur probante du log d'audit.

### 1. Sauvegarder le dossier `log/`

Le log d'activité a une rétention de 6 ans. Si le disque qui le contient lâche, **l'audit disparaît avec lui**. Pour un usage légal :

- Mettre `log/` sur un volume **régulièrement sauvegardé** (NAS, cloud, sauvegarde système Windows).
- Idéalement, copier `log/activity/*.log*` sur un autre support une fois par mois (les anciens fichiers ne changent plus, c'est trivial à automatiser).
- Si l'exe est dans `Program Files` (cas où SimpleClone bascule sur `%APPDATA%\SimpleClone\log\`), s'assurer que `%APPDATA%` du compte qui exécute l'app est bien dans le périmètre de sauvegarde.

Le chemin effectif est affiché dans le journal d'activité de l'application au démarrage — si vous avez un doute, ouvrez la fenêtre.

### 2. Vérifier que l'application tourne

Toutes les **15 minutes**, SimpleClone écrit une ligne `heartbeat` dans `log/activity/activity.log`. C'est la preuve positive qu'à cet instant le process était vivant.

**Vérification ponctuelle :**

```bash
# La dernière heartbeat doit dater de moins de 30 minutes
grep heartbeat log/activity/activity.log | tail -1
```

**Surveillance automatisée (recommandée) :** un script planifié (Tâches planifiées Windows, cron sur un poste tiers, supervision Centreon/Zabbix...) qui alerte si la dernière heartbeat date de plus d'une heure. Exemple en PowerShell :

```powershell
$last = Get-Content "C:\path\to\log\activity\activity.log" | Select-String heartbeat | Select-Object -Last 1
if (-not $last) { exit 1 }
$ts = ($last -match '"ts": "([^"]+)"') ; $datetime = [datetime]$matches[1]
if ((Get-Date) - $datetime -gt [TimeSpan]"01:00:00") { Write-Error "SimpleClone silencieux depuis > 1h" ; exit 1 }
```

### 3. Si l'opérateur signale un problème

L'utilisateur peut **générer un fichier de diagnostic** depuis l'application : clic sur le **bouton `?`** en haut à droite de la fenêtre, puis **"Créer un fichier de diagnostic"**. SimpleClone écrit alors un fichier `simpleclone-diagnostic-DATE-HEURE.txt` dans `log/diagnostic/`, et ouvre l'Explorateur de fichiers Windows directement sur ce fichier.

Le fichier contient :
- l'état courant (surveillance en cours / en pause / arrêtée)
- les chemins source et destination
- la version de SimpleClone, Python, plateforme
- la configuration JSON
- les **200 dernières lignes** du journal d'activité
- les **100 dernières lignes** du journal d'erreurs

L'opérateur copie le fichier sur une clé USB et l'envoie au support technique. Tout le contexte nécessaire est dedans, pas besoin que l'utilisateur sache lire un log.

### 4. En cas d'incident depuis le poste support

**"Le fichier X est manquant en destination."**

```bash
# Le fichier a-t-il été copié ?
grep "X" log/activity/activity.log*

# Si oui : le log donne l'horodatage exact, le chemin source et destination, la taille
# Si non : il n'a jamais été détecté côté source — vérifier la source
```

**"L'app était-elle bien active à la date Y ?"**

```bash
# Recherche la dernière heartbeat avant la date suspecte
grep heartbeat log/activity/activity.log.2026-04-* | tail
```

Une séquence `app_start` ... `heartbeat` ... `heartbeat` ... `app_stop` est le signe d'un cycle propre. Une absence de `app_stop` après la dernière `heartbeat` indique une **terminaison anormale** (crash, kill, coupure secteur) — informatif pour l'enquête.

### 5. Limitations connues à communiquer au client

- **Modifications directes sur la destination** : si l'utilisateur modifie un fichier directement sur la clé USB, il sera **écrasé** au prochain événement source. La destination est un miroir de la source, pas un dossier de travail.
- **Suppressions sur la source** : un fichier supprimé de la source est **archivé** dans `_Archive/` (jamais effacé définitivement par SimpleClone). Pour libérer de l'espace, c'est à l'utilisateur de purger manuellement `_Archive/`.
- **Démarrage automatique Windows** : la commande enregistrée dans le registre contient le **chemin absolu** de l'exécutable. Si vous déplacez l'exe, il faut décocher puis recocher "Démarrer avec Windows" pour mettre à jour le registre.
- **Intégrité des copies** : SimpleClone fait confiance à `shutil.copy2` (qui appelle l'API Windows native). Aucun checksum n'est calculé. Pour la majorité des cas c'est suffisant ; si votre cadre légal exige une preuve cryptographique d'intégrité, un complément (ex: hash sha256 stocké séparément) est nécessaire.
- **Pas de sync bidirectionnelle** : si la destination doit être préservée contre l'écrasement, n'utilisez pas SimpleClone.

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
