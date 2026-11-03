Monorepo: Node 20 services in services/, a React admin app in apps/admin and a Flutter app
in apps/mobile. Design notes live in docs/; read the ones for the area you change.

The agent container has 768 MB of memory. Do not run full test suites (`npm test` without
arguments) or e2e/; run only the test files you add or change, and leave the rest to CI.

CI on every pull request runs: unit tests for each service and the admin app; the e2e/
suite against the full docker-compose.e2e.yml stack; flutter analyze and flutter test; and
the integration tests on an Android emulator. A failure there comes back to you with the
failing step's log.
