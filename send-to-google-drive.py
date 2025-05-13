# send-to-google-drive.py
# python 3.10

"""
backup_multi_bot.py

Un service de backup multi-bot, chaque bot a sa propre config dans config.json.
Pour chaque bot, à chaque intervalle :
  1) zippe un dossier local en excluant certains fichiers/dossiers
  2) s’authentifie sur Google Drive
  3) crée un dossier horodaté sur Drive
  4) y upload le zip
  5) nettoie les anciennes sauvegardes locales et distantes
  6) journalise chaque étape dans un log plain-text très détaillé

Usage : python backup_multi_bot.py
Arrêt : Ctrl+C
"""

import os
import json
import yaml
import zipfile
import mimetypes
import time
import shutil
import threading
import logging
import traceback
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


class BotBackup:
    def __init__(self, cfg, global_cfg):
        # ==== Config spécifique au bot ====
        self.name                 = cfg["name"]
        self.service_account_file = cfg["service_account_file"]
        self.parent_folder_id     = cfg["parent_folder_id"]
        self.folder_to_zip        = cfg["folder_to_zip"]
        self.local_backup_root    = cfg.get("local_backup_root", f"backup/{self.name}")
        self.zip_prefix           = cfg.get("zip_prefix", f"backup-{self.name.lower()}")
        self.backup_interval      = cfg.get("backup_interval", global_cfg["backup_interval"])
        self.local_keep           = cfg.get("local_keep", global_cfg["local_keep"])
        self.drive_keep           = cfg.get("drive_keep", global_cfg["drive_keep"])
        # ==== Scopes et exclusions ====
        self.scopes               = global_cfg["scopes"]
        self.excluded_extensions  = set(cfg.get("excluded_extensions", global_cfg["excluded_extensions"]))
        self.excluded_folders     = set(cfg.get("excluded_folders",   global_cfg["excluded_folders"]))
        # ==== Initialisation du logger ====
        os.makedirs(self.local_backup_root, exist_ok=True)
        self._setup_logger()

    def _setup_logger(self):
        self.logger = logging.getLogger(self.name)
        self.logger.setLevel(logging.DEBUG)

        fmt = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # handler fichier
        log_path = os.path.join(self.local_backup_root, "backup.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)

        # handler console
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        # éviter les doublons
        if not self.logger.handlers:
            self.logger.addHandler(fh)
            self.logger.addHandler(ch)

        self.logger.info(f"[INIT] Logger initialisé, les logs vont dans : {log_path}")

    def authenticate(self):
        """ Authentification via compte de service """
        self.logger.debug("🔑 Authentification Google Service Account…")
        creds = service_account.Credentials.from_service_account_file(
            self.service_account_file, scopes=self.scopes
        )
        self.logger.debug("✔️ Authentifié")
        return creds

    def custom_zip_folder(self, source_folder, zip_path):
        """ Crée un ZIP en excluant extensions et dossiers configurés """
        self.logger.info(f"📦 Zipping '{source_folder}' → '{zip_path}'")
        if os.path.exists(zip_path):
            os.remove(zip_path)
            self.logger.debug("🗑️ Ancien zip supprimé")
        abs_src = os.path.abspath(source_folder)
        if not os.path.isdir(abs_src):
            raise FileNotFoundError(f"Dossier à zipper introuvable : {abs_src}")

        start = time.time()
        total_files = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(abs_src):
                dirs[:] = [d for d in dirs if d not in self.excluded_folders]
                for fname in files:
                    if any(fname.endswith(ext) for ext in self.excluded_extensions):
                        self.logger.debug(f"⛔ Exclu: {os.path.join(root, fname)}")
                        continue
                    full_path = os.path.join(root, fname)
                    arcname   = os.path.relpath(full_path, start=abs_src)
                    z.write(full_path, arcname)
                    total_files += 1
        duration = time.time() - start
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        self.logger.info(
            f"✔️ ZIP créé en {duration:.2f}s, {total_files} fichiers, taille {size_mb:.2f} MB"
        )

    def create_drive_folder(self, service, name):
        """ Crée un dossier Google Drive et retourne son ID """
        self.logger.info(f"📁 Création du dossier Drive '{name}'")
        meta = {
            "name":     name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents":  [self.parent_folder_id]
        }
        folder = service.files().create(body=meta, fields="id").execute()
        folder_id = folder["id"]
        self.logger.info(f"✔️ Dossier Drive créé: ID={folder_id}")
        return folder_id

    def upload_file_to_folder(self, service, folder_id, file_path):
        """ Upload d’un fichier sur Drive """
        self.logger.info(f"⏫ Upload de '{file_path}' → dossier Drive {folder_id}")
        mime_type, _ = mimetypes.guess_type(file_path)
        media = MediaFileUpload(
            file_path,
            mimetype=(mime_type or "application/octet-stream"),
            resumable=True
        )
        meta = {
            "name":    os.path.basename(file_path),
            "parents": [folder_id]
        }
        f = service.files().create(body=meta, media_body=media, fields="id").execute()
        file_id = f["id"]
        self.logger.info(f"✔️ Upload terminé : fileId={file_id}")
        return file_id

    def clean_local_backups(self):
        """ Nettoyage des backups locaux (garde les plus récents) """
        root = self.local_backup_root
        subs = [
            os.path.join(root, d)
            for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
        ]
        subs.sort(key=lambda p: os.path.getctime(p), reverse=True)
        to_remove = subs[self.local_keep:]
        for old in to_remove:
            shutil.rmtree(old)
            self.logger.info(f"🗑️ Supprimé local : {old}")

    def delete_old_backups_by_count(self, service):
        """ Nettoyage des dossiers Drive (garde les plus récents) """
        self.logger.debug("🔍 Récupération des dossiers Drive pour nettoyage")
        resp = service.files().list(
            q=(
                f"'{self.parent_folder_id}' in parents"
                " and mimeType='application/vnd.google-apps.folder'"
                " and trashed=false"
            ),
            fields="files(id,name,createdTime)",
            pageSize=1000
        ).execute()
        files = resp.get("files", [])
        files.sort(key=lambda f: f["createdTime"])
        to_delete = files[:-self.drive_keep]
        for f in to_delete:
            service.files().delete(fileId=f["id"]).execute()
            self.logger.info(
                f"🗑️ Supprimé Drive : {f['name']} (créé {f['createdTime']})"
            )

    def do_backup(self):
        """ Une passe complète de backup """
        now = datetime.now()
        # 1) Préparer dossier local horodaté
        jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
        mois  = ["janvier","février","mars","avril","mai","juin",
                 "juillet","août","septembre","octobre","novembre","décembre"]
        local_name = (
            f"{jours[now.weekday()]}-"
            f"{now.day:02d}-"
            f"{mois[now.month-1]}-"
            f"{now.year}-"
            f"{now.hour:02d}h{now.minute:02d}"
        )
        local_folder = os.path.join(self.local_backup_root, local_name)
        os.makedirs(local_folder, exist_ok=True)

        # 2) Nom du zip
        ts      = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        zip_name= f"{self.zip_prefix}_{ts}.zip"
        zip_path= os.path.join(local_folder, zip_name)

        self.logger.info("========== DÉBUT DE CYCLE DE BACKUP ==========")
        cycle_start = time.time()

        try:
            # 3) Zip
            self.custom_zip_folder(self.folder_to_zip, zip_path)

            # 4) Auth & service
            creds   = self.authenticate()
            service = build("drive", "v3", credentials=creds)

            # 5) Drive : création + upload
            drive_folder_name = f"{self.name} {now.day} {mois[now.month-1]} {now.year} {now.hour}h{now.minute:02d}"
            df_id = self.create_drive_folder(service, drive_folder_name)
            _   = self.upload_file_to_folder(service, df_id, zip_path)

            # 6) Nettoyages
            self.clean_local_backups()
            self.delete_old_backups_by_count(service)

            # Bilan
            duration = time.time() - cycle_start
            size_mb  = os.path.getsize(zip_path) / (1024 * 1024)
            self.logger.info(
                f"🎉 BACKUP OK en {duration:.2f}s — ZIP {size_mb:.2f} MB — "
                f"Prochain cycle dans {timedelta(**self.backup_interval)}"
            )

        except Exception as e:
            self.logger.error(f"❌ ERREUR pendant le backup : {e}")
            self.logger.debug(traceback.format_exc())

        self.logger.info("========== FIN DE CYCLE ==========\n")

    def run(self):
        interval = timedelta(**self.backup_interval).total_seconds()
        self.logger.info(f"🔄 Service lancé, intervalle = {interval/60:.1f} minutes")
        while True:
            try:
                self.do_backup()
            except Exception as e:
                self.logger.error(f"💥 Exception inattendue dans run(): {e}")
                self.logger.debug(traceback.format_exc())
            time.sleep(interval)


# def main():
#     # Charge la config centrale
#     with open("config.json", "r", encoding="utf-8") as f:
#         cfg = json.load(f)

def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Démarre un thread par bot
    for bot_cfg in cfg["bots"]:
        bot = BotBackup(bot_cfg, cfg)
        t = threading.Thread(target=bot.run, daemon=True)
        t.start()

    # Boucle principale pour attraper Ctrl+C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("🛑 Arrêt manuel de tous les services de backup.")


if __name__ == "__main__":
    main()
