/**
 * Tests for Aria's message queue ("add to queue") and steer features.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { CaliberAssistantPanel } from "@/components/assistant/CaliberAssistantPanel";
import { QueuedMessages } from "@/components/assistant/QueuedMessages";
import {
  AssistantPanelProvider,
  useAssistantPanel,
} from "@/components/assistant/AssistantPanelContext";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const ACTIVE_SESSION_KEY = "caliber.assistant.session.active";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function withProviders(node: JSX.Element): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        {node}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPanel(): void {
  function Inner(): JSX.Element {
    const { open, toggle } = useAssistantPanel();
    return (
      <>
        {!open && (
          <button type="button" onClick={toggle}>
            Open Assistant
          </button>
        )}
        <CaliberAssistantPanel />
      </>
    );
  }
  withProviders(
    <AssistantPanelProvider>
      <Inner />
    </AssistantPanelProvider>,
  );
}

function queuedItem(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    queue_id: "QMSG-1",
    session_id: "ASST-q1",
    content: "do this next",
    mode: "build",
    kind: "queued",
    position: 0,
    status: "pending",
    created_by: "@test",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("QueuedMessages", () => {
  it("renders nothing when empty and a steer badge when present", () => {
    const { rerender } = withProviders(<QueuedMessages items={[]} onCancel={() => {}} />);
    expect(screen.queryByTestId("assistant-queue")).not.toBeInTheDocument();

    rerender(
      <QueuedMessages
        items={[queuedItem({ kind: "steer", content: "change course" }) as never]}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByTestId("assistant-queue")).toHaveTextContent("change course");
    expect(screen.getByText("Steer")).toBeInTheDocument();
  });
});

describe("CaliberAssistantPanel — queue & steer", () => {
  async function openPanel(): Promise<void> {
    renderPanel();
    await userEvent.click(screen.getByText("Open Assistant"));
    await screen.findByText("Aria");
  }

  it("steer enqueues a priority message", async () => {
    let enqueued: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/assistant/sessions/:sessionId/queue`, async ({ request }) => {
        enqueued = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(queuedItem(enqueued)), { status: 201 });
      }),
    );

    await openPanel();
    await userEvent.type(screen.getByPlaceholderText("Ask Aria for follow-up changes..."), "redirect please");
    await userEvent.click(screen.getByRole("button", { name: "Steer" }));

    await waitFor(() => expect(enqueued).not.toBeNull());
    expect(enqueued).toMatchObject({ content: "redirect please", kind: "steer" });
  });

  it("auto-dispatches a queued message when no turn is running", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-q1");
    let dispatched = false;
    let sentContent: unknown = null;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-q1",
              title: "Queued session",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: {},
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () =>
        HttpResponse.json(envelope(dispatched ? [] : [queuedItem()])),
      ),
      http.delete(`${API_BASE}/assistant/queue/:queueId`, () => {
        dispatched = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sentContent = body.content;
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-q1",
              session_id: String(params.sessionId),
              role: "assistant",
              content: "ok",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        );
      }),
    );

    await openPanel();
    await waitFor(() => expect(sentContent).toBe("do this next"));
  });

  it("keeps a queued message when the send fails instead of deleting it", async () => {
    // Regression: the dispatcher issued a hard DELETE of the queue row *before*
    // sending, swallowed the DELETE's failure, and fired the send from a
    // `.finally()` so it ran on both branches. A send that then failed left the
    // user's typed message deleted server-side, absent from the panel, and with
    // no error shown. The row must survive a failed send.
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-q2");
    let deleted = false;
    let sendAttempts = 0;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-q2",
              title: "Queued session",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: {},
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(envelope([])),
      ),
      // The queue keeps reporting the row; nothing has legitimately removed it.
      http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () =>
        HttpResponse.json(envelope(deleted ? [] : [queuedItem()])),
      ),
      http.delete(`${API_BASE}/assistant/queue/:queueId`, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
        sendAttempts += 1;
        return HttpResponse.json({ error: "upstream unavailable" }, { status: 500 });
      }),
    );

    await openPanel();
    await waitFor(() => expect(sendAttempts).toBeGreaterThan(0));

    // The send failed, so the queue row must not have been destroyed.
    expect(deleted).toBe(false);
  });
});
