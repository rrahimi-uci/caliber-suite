# NATS (message bus) — optional

CALIBER can use NATS as its shared event fan-out backend for live SSE updates
and outbound webhook notifications across app replicas. Native local dev still
defaults to the in-process EventBus unless `CALIBER_WORKFLOW_RUN_EVENT_BACKEND`
is set to `nats`.

The service is profile-gated and can be run by itself:

```bash
docker compose -f deploy/compose.yaml --profile nats up -d
```

- Client: `nats://localhost:4222`
- Monitoring: <http://localhost:8222>

JetStream (`-js`) is on for future durable streams. The current CALIBER adapter
uses core NATS pub/sub for lightweight live fan-out.
