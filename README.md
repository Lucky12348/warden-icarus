# warden-icarus

Bot Discord qui administre le serveur **Icarus** dédié (image Docker `nerodon/icarus-dedicated`) sur la VM Debian 13.
Il remplace le bot intégré à l'ancien fork WindowsGSM et reprend ses panneaux persistants (Components V2), adaptés à Docker.

| Salon | Contenu |
|---|---|
| **Panneau** | État du conteneur, nom du serveur, joueurs (A2S), CPU/RAM, uptime, mis à jour toutes les 60 s. Boutons Démarrer / Arrêter / Redémarrer / Mettre à jour / Infos. |
| **Sauvegardes** | Liste des parties, sauvegarde manuelle (archive `.tar.gz` horodatée, avec rétention), restauration avec confirmation. |
| **Réglages** | Interrupteurs d'alertes, redémarrage planifié, variables d'Icarus (nom, mots de passe masqués, joueurs max…) modifiables, puis recréation du conteneur. |
| **Logs** | Qui a fait quoi, lignes importantes du conteneur (anti-spam), arrivées et départs des joueurs, alertes avec mention du rôle admin. |

Slash commands : `/aide`, `/statut`, `/stats` (pour tous) ; `/demarrer`, `/arreter`, `/redemarrer`, `/maj`, `/sauvegarde`, `/panneaux` (rôle admin).
Infos, `/statut` et `/stats` sont en lecture seule et ouverts à tous. Toutes les autres actions demandent le rôle admin et sont journalisées dans le salon logs.

---

## 1. Installation sur la VM

Toutes les commandes se lancent **sur la VM** (`ssh lucky@192.168.1.109`), en tant que `lucky`.

### 1.1 Prérequis

```bash
docker --version && docker compose version   # Docker et le plugin compose sont déjà là pour Icarus
id -nG | grep -qw docker && echo "OK: lucky est dans le groupe docker"
```

### 1.2 Créer le bot Discord

1. <https://discord.com/developers/applications> → **New Application** (par exemple « Warden Icarus »).
2. Onglet **Bot** → **Reset Token** → copier le token (il servira pour `DISCORD_TOKEN`). Aucun *Privileged Gateway Intent* n'est nécessaire, laisse-les tous désactivés.
3. Onglet **OAuth2 → URL Generator** : scopes `bot` et `applications.commands`. Permissions : *View Channels*, *Send Messages*, *Read Message History*, *Mention Everyone* (pour mentionner le rôle admin dans les alertes). Ouvre l'URL générée et invite le bot sur ton serveur.
   Le lien d'invitation est aussi écrit dans les logs du bot à chaque démarrage.
4. Dans Discord, active le **mode développeur** (Paramètres → Avancés), puis copie par clic droit :
   - l'ID du serveur (`GUILD_ID`) ;
   - l'ID du rôle admin (Paramètres du serveur → Rôles → clic droit → `ADMIN_ROLE_ID`).
5. Vérifie que le bot peut **voir les 4 salons, y écrire et y lire l'historique**.

> Si l'ancien bot WindowsGSM utilisait **le même compte bot**, arrête-le d'abord. Au premier lancement, warden-icarus supprime ses propres anciens messages dans chaque salon de panneau, ce qui retire les panneaux de l'ancien bot. Si c'était **un autre compte**, supprime les anciens panneaux à la main.

### 1.3 Adapter le compose d'Icarus (une seule fois)

Le bot ne réécrit que `/home/lucky/icarus/.env`. Le `docker-compose.yml` doit donc lire chaque réglage depuis le `.env` (`${VAR}`). Les valeurs ci-dessous sont **identiques** à ta configuration actuelle. On ajoute seulement `stop_grace_period: 90s` pour que le jeu ait le temps de sauvegarder.

Commence par récupérer le code du bot :

```bash
cd ~
git clone https://github.com/Lucky12348/warden-icarus.git   # adapte l'URL à ton dépôt
```

Puis migre le compose d'Icarus. Les originaux sont conservés en `.orig` :

```bash
cd /home/lucky/icarus
cp docker-compose.yml docker-compose.yml.orig
cp .env .env.orig
cat ~/warden-icarus/deploy/icarus/env.additions >> .env
cp ~/warden-icarus/deploy/icarus/docker-compose.yml docker-compose.yml
chmod 600 .env
mkdir -p backups
docker compose config --quiet && echo "compose OK"
docker compose up -d        # recrée le conteneur avec la même configuration
```

Vérifie que les variables sont bien prises en compte (les mots de passe ne s'affichent pas) :

```bash
docker inspect icarus --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(SERVERNAME|MAX_PLAYERS|SHUTDOWN_EMPTY_FOR)='
```

### 1.4 Installer le bot

```bash
cd ~/warden-icarus
cp .env.example .env
chmod 600 .env
getent group docker | cut -d: -f3        # → à mettre dans DOCKER_GID
nano .env                                # DISCORD_TOKEN, GUILD_ID, ADMIN_ROLE_ID, DOCKER_GID (+ ntfy si voulu)
mkdir -p state                           # doit appartenir à lucky (uid 1000), pas à root
docker compose up -d --build
docker compose logs -f                   # Ctrl+C pour quitter le suivi
```

Les logs doivent afficher `Connecté en tant que …` puis `… slash commands synchronisées`. Dans Discord, les trois panneaux apparaissent et le salon logs reçoit « warden-icarus démarré ».

### 1.5 ntfy (optionnel)

Dans `~/warden-icarus/.env` :

```dotenv
NTFY_URL=https://ntfy.sh          # ou ton instance
NTFY_TOKEN=tk_xxxxxxxx            # si le topic est protégé
NTFY_TOPIC=icarus
```

Applique avec `docker compose up -d`, puis active l'interrupteur **ntfy** dans le salon réglages.

---

## 2. Fonctionnement

### Panneau serveur
- **Mettre à jour** = redémarrer le conteneur, puisque l'image installe la dernière version d'Icarus à chaque démarrage. Le bot compare le *build* Steam (`game/steamapps/appmanifest_2089300.acf`) et publie « Mise à jour installée » quand il change.
- Les arrêts laissent au jeu **au moins 60 s** (`STOP_TIMEOUT`) pour sauvegarder.
- Si des joueurs sont connectés, Arrêter / Redémarrer / Mettre à jour demandent une confirmation.
- Une seule opération à la fois : les autres boutons attendent qu'elle se termine.

### Joueurs
A2S interroge `127.0.0.1:32784` toutes les 20 s. Si Icarus ne renvoie pas les noms, le panneau affiche « 2/8 joueurs » et le salon logs annonce « Un joueur a rejoint la partie ».
Une détection par les logs peut être activée plus tard (`PLAYER_DETECTION=logs` ou `both`, avec des motifs `join`/`leave` dans un fichier de motifs, voir plus bas).

### Sauvegardes
- **Archives** : `/home/lucky/icarus/backups/icarus-AAAAMMJJ-HHMMSS.tar.gz`. Chaque archive contient tout `data/Saved/PlayerData` (parties et données des joueurs), sans les `.backup_N` du jeu, et s'accompagne d'un fichier `.manifest.json`. Seules les `BACKUP_RETENTION` plus récentes sont conservées. Une sauvegarde est possible serveur allumé : chaque `.json` est relu jusqu'à être un JSON valide.
- **Restauration** : choix de la partie, puis de la sauvegarde (`.backup_N` du jeu, `.pre_restore_*` ou archive du bot), puis confirmation. Le bot arrête le serveur, copie la partie actuelle en `<partie>.json.pre_restore_<date>`, la remplace par la sauvegarde choisie (qui est conservée), puis redémarre le serveur. Une sauvegarde au JSON invalide est refusée et rien n'est modifié.

### Réglages
- Les réglages sont choisis dans le menu, puis saisis dans un modal et validés (bornes, True/False).
- Avant de modifier quoi que ce soit, le bot affiche une confirmation avec trois choix : **Appliquer et recréer**, **Enregistrer seulement** ou **Annuler**.
- Le `.env` d'Icarus est réécrit proprement : commentaires et ordre conservés, copie `.env.bak`, écriture atomique, puis vérification par `docker compose config`. Si quelque chose échoue, le `.env.bak` est restauré.
- Les mots de passe ne sont **jamais affichés** : « •••••• (défini) ». Dans le modal, un champ vide laisse le mot de passe inchangé et `-` seul le supprime.
- ⏳ signale un réglage modifié mais pas encore appliqué (le `.env` diffère du conteneur en cours d'exécution). 🔒 signale un réglage écrit en dur dans le compose.

### Alertes
| Événement | Comment il est détecté |
|---|---|
| 💥 Crash | Événement Docker `die` avec un code de sortie ≠ 0 (ou `oom`), sans action du bot en cours |
| 🔁 Redémarrage inattendu | Relance par Docker après un crash, ou `docker restart` lancé en dehors de Discord |
| ⬆️ Mise à jour | Le build Steam installé a changé |

Les alertes mentionnent le rôle admin dans le salon logs et partent aussi sur ntfy si l'interrupteur est activé. Elles passent par un anti-spam (5 alertes par 10 min au maximum, même type au plus toutes les 2 min).
Le cycle normal de l'image ne déclenche **aucune** alerte : serveur vide, arrêt propre (code 0) via `SHUTDOWN_EMPTY_FOR`, puis relance par Docker. Si ton serveur sort avec un autre code lors de cet arrêt automatique, ajoute ce code à `CLEAN_EXIT_CODES` (par exemple `0,1`).

### Redémarrage planifié
Activé par défaut à **06:00 (Europe/Paris)**, l'heure se change dans le salon réglages. Le bot l'annonce dans le salon logs 5 min avant. Si des joueurs sont connectés, ou si A2S ne répond pas, il ne redémarre pas et réessaie toutes les 15 min pendant 2 h au maximum, puis abandonne pour la journée. Un serveur déjà arrêté n'est pas relancé.

### Logs du conteneur
Seules les lignes importantes sont diffusées : démarrage, mise à jour, erreurs, crash. Le bruit normal de Wine et d'Unreal (`fixme:`/`err:`, `setlocale(`, `XDG_RUNTIME_DIR`, `LogFMOD:`, `LogStreaming: Error`…) est ignoré grâce à la catégorie `ignore` : ces lignes ne sont ni diffusées ni comptées par l'anti-spam. Les lignes sont regroupées toutes les 5 s, avec deux limites anti-spam : une même ligne au plus toutes les 5 min (`LOG_DEDUPE_SECONDS`) et au plus 10 lignes par minute (`LOG_MAX_PER_MINUTE`). Le nombre de lignes ignorées est indiqué.
Pour changer les motifs, copie le fichier d'exemple dans le dossier d'état :

```bash
cp ~/warden-icarus/patterns.example.json ~/warden-icarus/state/patterns.json
nano ~/warden-icarus/state/patterns.json       # catégories : ignore, crash, join, leave, update, startup, error
echo 'LOG_PATTERNS_FILE=/app/state/patterns.json' >> ~/warden-icarus/.env
cd ~/warden-icarus && docker compose up -d
```

Chaque catégorie présente dans le fichier remplace celle par défaut (si tu définis `ignore`, garde les motifs de l'exemple et ajoute les tiens ; sans clé `ignore`, les motifs par défaut s'appliquent). Pour `join`/`leave`, le groupe nommé `(?P<name>…)` capture le nom du joueur.

### Sécurité
- Le socket Docker **équivaut à root sur la VM**. Le bot ne l'utilise qu'à travers `warden/docker_ctl.py`, qui n'agit que sur le conteneur nommé `icarus` et seulement pour lire son état, ses statistiques, ses logs et ses événements, le démarrer, l'arrêter, le redémarrer ou le recréer. Pour une recréation, le bot vérifie que le projet compose se trouve bien dans `/home/lucky/icarus`. Pas d'`exec`, pas d'autre conteneur. Cette limite est assurée par le code, pas par Docker : protège le token Discord et l'accès à la VM en conséquence.
- Le conteneur du bot tourne avec l'uid 1000, le groupe docker de l'hôte, un système de fichiers en lecture seule, `no-new-privileges` et aucune capability.
- Les secrets (`DISCORD_TOKEN`, `NTFY_TOKEN`, mots de passe d'Icarus) restent dans des `.env` en `chmod 600`, jamais dans Git ni sur Discord.

---

## 3. Mise à jour du bot

```bash
cd ~/warden-icarus
git pull
docker compose up -d --build
docker compose logs --tail=50
```

Retour à la version précédente : `git log --oneline`, puis `git checkout <commit>` et `docker compose up -d --build`.
L'état du bot (interrupteurs, heure planifiée, IDs des panneaux) est conservé dans `state/state.json`.

---

## 4. Dépannage

| Symptôme | Cause probable / solution |
|---|---|
| `DOCKER_GID manquant` au `docker compose up` | Renseigne `DOCKER_GID` dans `.env` (`getent group docker \| cut -d: -f3`). |
| `Configuration invalide : …` dans les logs | Variable obligatoire absente ou mal formée dans `.env`. Le message indique laquelle. |
| `Permission denied` sur `/var/run/docker.sock` | `DOCKER_GID` ne correspond pas au groupe propriétaire du socket : `stat -c %g /var/run/docker.sock`. |
| `PermissionError` sur `state/state.json` | `state/` a été créé par root : `sudo chown -R 1000:1000 ~/warden-icarus/state`. |
| Les panneaux n'apparaissent pas | Le bot doit voir le salon, y écrire et lire l'historique. Vérifie aussi les IDs de salon. `/panneaux` force la republication. |
| Les slash commands n'apparaissent pas | Vérifie `GUILD_ID` et que le bot a été invité avec le scope `applications.commands`. Recharge Discord (Ctrl+R). |
| « Joueurs : — (le serveur ne répond pas) » | Normal pendant le démarrage ou la mise à jour (jusqu'à 15 min). Sinon teste A2S depuis la VM : `docker exec warden-icarus python -c "import a2s;print(a2s.info(('127.0.0.1',32784)))"`. |
| Mention du rôle admin sans notification | Donne au bot la permission *Mentionner @everyone…* ou rends le rôle mentionnable. |
| Réglage marqué 🔒 | Le réglage est écrit en dur dans le `docker-compose.yml` d'Icarus : remplace la valeur par `${VAR}` et ajoute `VAR=…` au `.env` (voir 1.3). |
| La recréation échoue | Lis le message dans Discord. À la main : `cd /home/lucky/icarus && docker compose config --quiet && docker compose up -d`. Le `.env` précédent est dans `.env.bak`. |
| Fausses alertes « Crash » toutes les ~5 min | L'arrêt automatique d'Icarus sort avec un code ≠ 0 : ajoute ce code (visible dans l'alerte) à `CLEAN_EXIT_CODES`. |
| Trop ou pas assez de logs | Ajuste `LOG_MAX_PER_MINUTE` et `LOG_DEDUPE_SECONDS`, ou les motifs (voir « Logs du conteneur »). |
| Erreur ou bouton « Échec de l'interaction » | `docker compose logs --tail=100 warden`. Les erreurs y sont détaillées. |

Commandes utiles :

```bash
cd ~/warden-icarus
docker compose ps                         # état du bot (healthy ?)
docker compose logs -f --tail=100         # logs du bot
docker compose restart                    # redémarrer le bot
sed -i 's/^LOG_LEVEL=.*/LOG_LEVEL=DEBUG/' .env && docker compose up -d   # plus de détails (remettre INFO ensuite)
docker logs --tail=100 icarus             # logs bruts du serveur Icarus
```

---

## 5. Développement

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                    # sauvegardes, réglages, anti-spam, planification, événements…
```

Arborescence :

```
warden/
  __main__.py      point d'entrée (python -m warden)
  config.py        configuration (.env du bot)
  bot.py           client Discord, panneaux, boucles (joueurs, événements, logs, planification)
  handlers.py      boutons, menus, modals (serveur, sauvegardes, réglages)
  commands.py      slash commands
  panels.py        contenu des panneaux
  ui.py            rendu Components V2
  docker_ctl.py    accès Docker restreint au conteneur icarus
  backups.py       archives, rétention, restauration
  settings_store.py lecture/écriture du .env d'Icarus
  events.py        classification des événements Docker (crash, redémarrage inattendu…)
  scheduler.py     redémarrage planifié
  players.py       A2S et détection des arrivées/départs
  logpatterns.py   tri des lignes de logs
  antispam.py      anti-spam
  notify.py        salon logs et ntfy
  state.py         état persistant (state/state.json)
deploy/icarus/     compose et .env d'Icarus adaptés au bot
tests/             tests unitaires (pytest)
```
