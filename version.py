"""
Single source of truth para a versao do ScalperFlow Bot.

Sempre que liberar uma nova versao:
1. Bumpar __version__ aqui (semver: MAJOR.MINOR.PATCH)
2. git commit + git tag vX.Y.Z + git push --tags
3. A Action valida que a tag bate com este valor antes de publicar.
"""
__version__ = "1.0.15"
