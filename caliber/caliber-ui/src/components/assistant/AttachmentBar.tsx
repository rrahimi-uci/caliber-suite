/**
 * AttachmentBar — Aria's "+ add files" affordance. Lets the user attach context
 * to the conversation from four sources (upload, object store, library resource,
 * pasted text), shows attached items as removable chips, and forwards everything
 * to the session's attachment API. Context is injected into Aria's prompt.
 */

import { useRef, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  AssistantAttachment,
  LibraryResourceType,
} from "@/api/assistantTypes";
import { ATTACHMENT_KIND_LABELS, LIBRARY_RESOURCE_TYPES } from "@/api/assistantTypes";
import { useApiQuery, useApiMutation, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import { cn } from "@/lib/utils";

export const attachmentsKey = (sessionId: string | null) =>
  ["assistant", "attachments", sessionId ?? "none"] as const;

interface AttachmentBarProps {
  sessionId: string | null;
  /** Resolve (creating if needed) the session to attach to. */
  ensureSession: () => Promise<string>;
  disabled?: boolean;
}

type ActiveModal = "text" | "library" | "object" | null;

export function AttachmentBar({
  sessionId,
  ensureSession,
  disabled,
}: AttachmentBarProps): JSX.Element {
  const invalidate = useInvalidate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [modal, setModal] = useState<ActiveModal>(null);

  const { data: attachments = [] } = useApiQuery<AssistantAttachment[]>(
    attachmentsKey(sessionId),
    () => (sessionId ? caliberApi.listAssistantAttachments(sessionId) : Promise.resolve([])),
    { enabled: !!sessionId },
  );

  const refresh = (sid: string) => invalidate(attachmentsKey(sid));

  const remove = useApiMutation(
    (attachmentId: string) => caliberApi.deleteAssistantAttachment(attachmentId),
    {
      onSuccess() {
        if (sessionId) refresh(sessionId);
      },
    },
  );

  const upload = useApiMutation(
    async (file: File) => {
      const sid = await ensureSession();
      const att = await caliberApi.uploadAssistantAttachment(sid, file);
      return { att, sid };
    },
    {
      onSuccess({ sid }) {
        refresh(sid);
        showToast.success("File attached");
      },
      onError(err) {
        showToast.error(err.message || "Upload failed");
      },
    },
  );

  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) upload.mutate(file);
    e.target.value = "";
  };

  const menuItems: { key: ActiveModal | "upload"; label: string; icon: JSX.Element }[] = [
    { key: "upload", label: "Upload file", icon: <IconUpload /> },
    { key: "object", label: "Object store", icon: <IconBucket /> },
    { key: "library", label: "Library resource", icon: <IconLibrary /> },
    { key: "text", label: "Paste text", icon: <IconText /> },
  ];

  return (
    <div className="space-y-1.5">
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5" data-testid="assistant-attachment-chips">
          {attachments.map((att) => (
            <span
              key={att.attachment_id}
              title={`${ATTACHMENT_KIND_LABELS[att.kind]} · ${att.bytes_size} bytes${att.truncated ? " (truncated)" : ""}`}
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 pl-2 pr-1 py-0.5 text-[11px] text-slate-600 dark:text-slate-200 max-w-[180px]"
            >
              <span className="truncate">{att.name}</span>
              <button
                type="button"
                aria-label={`Remove ${att.name}`}
                onClick={() => remove.mutate(att.attachment_id)}
                className="rounded-full p-0.5 text-slate-400 hover:text-red-500 dark:hover:text-red-300"
              >
                <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="relative inline-block">
        <button
          type="button"
          data-testid="assistant-add-context"
          disabled={disabled || upload.isPending}
          onClick={() => setMenuOpen((v) => !v)}
          className="inline-flex items-center gap-1 rounded-lg border border-dashed border-slate-300 dark:border-slate-600 px-2 py-1 text-xs font-medium text-slate-500 dark:text-slate-300 hover:border-caliber-400 hover:text-caliber-600 dark:hover:text-caliber-300 disabled:opacity-50"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 5v14M5 12h14" />
          </svg>
          {upload.isPending ? "Attaching…" : "Add context"}
        </button>

        {menuOpen && (
          <div className="absolute bottom-full mb-1 left-0 z-20 w-44 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-lg py-1">
            {menuItems.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  if (item.key === "upload") fileInputRef.current?.click();
                  else setModal(item.key);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-600 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800"
              >
                <span className="text-slate-400">{item.icon}</span>
                {item.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <input
        ref={fileInputRef}
        type="file"
        aria-label="Upload a file to attach"
        title="Upload a file to attach"
        className="hidden"
        onChange={onFilePicked}
      />

      {modal === "text" && (
        <TextSnippetModal
          ensureSession={ensureSession}
          onClose={() => setModal(null)}
          onAdded={refresh}
        />
      )}
      {modal === "library" && (
        <LibraryPickerModal
          ensureSession={ensureSession}
          onClose={() => setModal(null)}
          onAdded={refresh}
        />
      )}
      {modal === "object" && (
        <ObjectPickerModal
          ensureSession={ensureSession}
          onClose={() => setModal(null)}
          onAdded={refresh}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Modals                                                             */
/* ------------------------------------------------------------------ */

interface ModalBaseProps {
  ensureSession: () => Promise<string>;
  onClose: () => void;
  onAdded: (sessionId: string) => void;
}

function ModalShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-md rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-700 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="p-1 rounded-md text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}

function TextSnippetModal({ ensureSession, onClose, onAdded }: ModalBaseProps): JSX.Element {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const add = useApiMutation(
    async () => {
      const sid = await ensureSession();
      await caliberApi.createAssistantAttachment(sid, {
        kind: "text_snippet",
        name: name.trim() || undefined,
        text,
      });
      return sid;
    },
    {
      onSuccess(sid) {
        onAdded(sid);
        showToast.success("Text attached");
        onClose();
      },
      onError(err) {
        showToast.error(err.message || "Failed to attach text");
      },
    },
  );

  return (
    <ModalShell title="Paste text" onClose={onClose}>
      <div className="space-y-3" data-testid="assistant-text-modal">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Label (optional)"
          className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
        />
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder="Paste context here…"
          className="w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
        />
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => add.mutate(undefined)}
            disabled={!text.trim() || add.isPending}
            className="rounded-lg bg-caliber-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-caliber-700 disabled:opacity-50"
          >
            {add.isPending ? "Attaching…" : "Attach"}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

const LIBRARY_LABELS: Record<LibraryResourceType, string> = {
  prompt: "Prompts",
  skill: "Skills",
  tool: "Tools",
  workflow: "Workflows",
  knowledge_base: "Knowledge",
};

function LibraryPickerModal({ ensureSession, onClose, onAdded }: ModalBaseProps): JSX.Element {
  const [type, setType] = useState<LibraryResourceType>("skill");

  const { data: items = [], isLoading, error } = useApiQuery<{ id: string; label: string }[]>(
    ["assistant", "library-picker", type],
    async (signal) => {
      switch (type) {
        case "skill":
          return (await caliberApi.listSkills({}, signal)).map((s) => ({ id: s.skill_id, label: s.name }));
        case "tool":
          return (await caliberApi.listTools(undefined, signal)).map((t) => ({ id: t.tool_id, label: `${t.name} v${t.version}` }));
        case "workflow":
          return (await caliberApi.listWorkflows(undefined, signal)).map((w) => ({ id: w.workflow_id, label: w.name }));
        case "knowledge_base":
          return (await caliberApi.listKnowledgeBases({}, signal)).map((k) => ({ id: k.knowledge_base_id, label: k.name }));
        case "prompt":
          return (await caliberApi.listPrompts(signal))
            .filter((p) => !!p.prompt_name)
            .map((p) => ({ id: p.prompt_name as string, label: p.prompt_name as string }));
        default:
          return [];
      }
    },
  );

  const add = useApiMutation(
    async (resourceId: string) => {
      const sid = await ensureSession();
      await caliberApi.createAssistantAttachment(sid, {
        kind: "library_resource",
        resource_type: type,
        resource_id: resourceId,
      });
      return sid;
    },
    {
      onSuccess(sid) {
        onAdded(sid);
        showToast.success("Resource attached");
        onClose();
      },
      onError(err) {
        showToast.error(err.message || "Failed to attach resource");
      },
    },
  );

  return (
    <ModalShell title="Attach library resource" onClose={onClose}>
      <div className="space-y-3" data-testid="assistant-library-modal">
        <div className="flex flex-wrap gap-1">
          {LIBRARY_RESOURCE_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setType(t)}
              className={cn(
                "rounded-md px-2 py-1 text-[11px] font-medium",
                type === t
                  ? "bg-caliber-600 text-white"
                  : "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700",
              )}
            >
              {LIBRARY_LABELS[t]}
            </button>
          ))}
        </div>
        <div className="max-h-64 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-700">
          {isLoading ? (
            <p className="p-3 text-xs text-slate-400">Loading…</p>
          ) : error ? (
            <p className="p-3 text-xs text-red-600">
              Couldn&apos;t load {LIBRARY_LABELS[type].toLowerCase()}: {error.message}
            </p>
          ) : items.length === 0 ? (
            <p className="p-3 text-xs text-slate-400">No {LIBRARY_LABELS[type].toLowerCase()} found.</p>
          ) : (
            items.map((item) => (
              <button
                key={item.id}
                type="button"
                disabled={add.isPending}
                onClick={() => add.mutate(item.id)}
                className="flex w-full items-center justify-between border-b border-slate-100 dark:border-slate-800 px-3 py-2 text-left text-xs text-slate-700 dark:text-slate-200 last:border-b-0 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50"
              >
                <span className="truncate">{item.label}</span>
                <span className="text-caliber-600 dark:text-caliber-300">Attach</span>
              </button>
            ))
          )}
        </div>
      </div>
    </ModalShell>
  );
}

function ObjectPickerModal({ ensureSession, onClose, onAdded }: ModalBaseProps): JSX.Element {
  const [bucket, setBucket] = useState<string>("");

  const { data: buckets = [] } = useApiQuery(
    ["assistant", "object-buckets"],
    (signal) => caliberApi.listObjectStoreBuckets(signal),
  );

  const { data: listing, isLoading, error } = useApiQuery(
    ["assistant", "object-objects", bucket],
    (signal) => caliberApi.listObjectStoreObjects(bucket, "", {}, signal),
    { enabled: !!bucket },
  );

  const add = useApiMutation(
    async (key: string) => {
      const sid = await ensureSession();
      await caliberApi.createAssistantAttachment(sid, { kind: "object_file", bucket, key });
      return sid;
    },
    {
      onSuccess(sid) {
        onAdded(sid);
        showToast.success("Object attached");
        onClose();
      },
      onError(err) {
        showToast.error(err.message || "Failed to attach object");
      },
    },
  );

  return (
    <ModalShell title="Attach object-store file" onClose={onClose}>
      <div className="space-y-3" data-testid="assistant-object-modal">
        <label className="block">
          <span className="text-xs font-medium text-slate-500 dark:text-slate-300">Bucket</span>
          <select
            value={bucket}
            onChange={(e) => setBucket(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
          >
            <option value="">Select a bucket…</option>
            {buckets.map((b) => (
              <option key={b.name} value={b.name}>
                {b.name}
              </option>
            ))}
          </select>
        </label>

        {bucket && (
          <div className="max-h-64 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-700">
            {isLoading ? (
              <p className="p-3 text-xs text-slate-400">Loading…</p>
            ) : error ? (
              <p className="p-3 text-xs text-red-600">Couldn&apos;t load objects: {error.message}</p>
            ) : !listing || listing.objects.length === 0 ? (
              <p className="p-3 text-xs text-slate-400">No files at the bucket root.</p>
            ) : (
              listing.objects.map((obj) => (
                <button
                  key={obj.key}
                  type="button"
                  disabled={add.isPending}
                  onClick={() => add.mutate(obj.key)}
                  className="flex w-full items-center justify-between border-b border-slate-100 dark:border-slate-800 px-3 py-2 text-left text-xs text-slate-700 dark:text-slate-200 last:border-b-0 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50"
                >
                  <span className="truncate">{obj.key}</span>
                  <span className="text-caliber-600 dark:text-caliber-300">Attach</span>
                </button>
              ))
            )}
          </div>
        )}
      </div>
    </ModalShell>
  );
}

/* ------------------------------------------------------------------ */
/* Icons                                                              */
/* ------------------------------------------------------------------ */

function IconUpload(): JSX.Element {
  return (
    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  );
}

function IconBucket(): JSX.Element {
  return (
    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}

function IconLibrary(): JSX.Element {
  return (
    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" />
    </svg>
  );
}

function IconText(): JSX.Element {
  return (
    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="4" y1="7" x2="20" y2="7" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="17" x2="14" y2="17" />
    </svg>
  );
}
