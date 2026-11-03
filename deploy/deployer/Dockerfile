# The Dev Factory deployer (docs/deployer.md): runs on the production host with the Docker
# socket and applies approved release manifests. It needs only the docker CLI and Python;
# the factory's Claude, gh and git tooling are not in this image.
FROM docker:29.1.3-cli
# Links the published image to this repository on GitHub.
LABEL org.opencontainers.image.source="https://github.com/abegalinov/dev-factory"

RUN apk add --no-cache python3 py3-yaml \
 && printf '#!/bin/sh\nexec python3 -m dev_factory.cli deployer "$@"\n' > /usr/local/bin/deployer \
 && chmod +x /usr/local/bin/deployer

COPY src/dev_factory /opt/dev-factory/dev_factory
# Its own name, so `deployer install` starts the deployer from the same image.
ARG IMAGE=""
ENV PYTHONPATH=/opt/dev-factory PYTHONUNBUFFERED=1 DEPLOYER_IMAGE=${IMAGE}

# `docker exec dev-factory-deployer deployer status` runs the same command (exec skips this).
ENTRYPOINT ["deployer"]
CMD ["run"]
