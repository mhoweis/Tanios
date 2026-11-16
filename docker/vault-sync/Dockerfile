# Obsidian Headless: Obsidian's own command-line client for Obsidian Sync.
# It keeps the Tanios vault on this server in step with the owner's phone and computer.
FROM node:22-slim

# Obsidian Headless is in open beta. The version is pinned so an update is a choice.
# To update: change the number, then run  docker compose --profile sync build vault-sync
ARG OBSIDIAN_HEADLESS_VERSION=0.0.14
RUN npm install -g obsidian-headless@${OBSIDIAN_HEADLESS_VERSION} \
    && npm cache clean --force

# The same user number as the app container, so both can read and write the notes.
# /state holds the Obsidian sign-in and sync bookkeeping.
RUN useradd --system --uid 10001 --home-dir /state --no-create-home tanios \
    && mkdir /vaults /state \
    && chown tanios /vaults /state
ENV HOME=/state \
    XDG_CONFIG_HOME=/state/config
USER tanios
WORKDIR /vaults

CMD ["ob", "sync", "--path", "/vaults/user-1", "--continuous"]
