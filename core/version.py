"""
core/version.py
────────────────
Single source of truth for product identity. build_exe.py stamps these into
the EXE's version resource and passes them to the Inno Setup compiler, so a
release only needs the version bumped here.
"""

# APP_NAME names folders and files (%LOCALAPPDATA%, Documents, Setup.exe), so
# it has no spaces; APP_TITLE is what people see.
APP_NAME    = "AditsVoiceStudio"
APP_TITLE   = "Adits Voice Studio"
EXE_NAME    = "AditsVoiceStudio"            # dist/<EXE_NAME>/<EXE_NAME>.exe
__version__ = "1.0.0"
PUBLISHER   = "iftiaj.com"
SOURCE_URL  = "https://github.com/iftiaj-com/AditsVoiceStudio"
LICENSE_ID  = "GPL-3.0-or-later"

# Names used before the rebrand. core/paths.py moves the old data folder to
# the new one once; installer.iss upgrades an install that has the old EXE.
LEGACY_APP_NAME = "VoiceCraft"
LEGACY_EXE_NAME = "TTS_Studio"
