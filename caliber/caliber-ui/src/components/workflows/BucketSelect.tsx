/**
 * Bucket picker for object-storage I/O nodes (input_bucket / output_bucket).
 *
 * Lists existing buckets from the Object Store and lets the user either pick one
 * or create a new bucket inline — so mapping a node to a bucket never requires
 * leaving the workflow editor. A bucket named on the node but absent from the
 * account is surfaced as "missing" with a one-click create.
 */

import { useCallback, useRef, useState } from "react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { useApi } from "@/hooks/useApi";
import { useApiQuery } from "@/hooks/useApiQuery";
import { buildCaliberRouteHref } from "@/lib/externalLinks";

/**
 * Whether the signed-in user may create/upload to the object store. Reads (list
 * buckets/objects) are open to any authenticated user; create-bucket,
 * create-folder, and upload all require the admin scope server-side, so the
 * write affordances are hidden for non-admins. ``/me`` is cached app-wide by
 * react-query, so calling this from several components issues one request.
 */
function useCanManageStorage(): boolean {
  const { data } = useApiQuery(["me"], (s) => caliberApi.getMe(s));
  return data?.is_admin ?? false;
}

const inputClass =
  "w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 placeholder:text-zinc-400";
const selectClass = inputClass;

interface BucketSelectProps {
  /** Currently mapped bucket name (may be empty or not-yet-created). */
  value: string;
  onChange: (bucket: string) => void;
  testId?: string;
}

const CREATE_SENTINEL = "__create__";

export function BucketSelect({
  value,
  onChange,
  testId,
}: BucketSelectProps): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listObjectStoreBuckets(signal),
    [],
  );
  const { data, loading, error, refresh } = useApi(fetcher);
  const canManage = useCanManageStorage();

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const buckets = data ?? [];
  const knownNames = buckets.map((b) => b.name);
  const missing = value !== "" && !loading && !knownNames.includes(value);

  const handleSelect = (selected: string): void => {
    if (selected === CREATE_SENTINEL) {
      setCreating(true);
      setNewName("");
      setCreateError(null);
      return;
    }
    onChange(selected);
  };

  const handleCreate = async (): Promise<void> => {
    const name = newName.trim();
    if (!name) {
      setCreateError("Enter a bucket name");
      return;
    }
    setBusy(true);
    setCreateError(null);
    try {
      await caliberApi.createObjectStoreBucket(name);
      onChange(name);
      setCreating(false);
      refresh();
    } catch (err) {
      // 409 = already exists: adopt it rather than erroring out.
      if (err instanceof ApiError && err.status === 409) {
        onChange(name);
        setCreating(false);
        refresh();
        return;
      }
      setCreateError(
        err instanceof ApiError ? err.message : "Failed to create bucket",
      );
    } finally {
      setBusy(false);
    }
  };

  const createMissing = async (): Promise<void> => {
    setBusy(true);
    try {
      await caliberApi.createObjectStoreBucket(value);
      refresh();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 409)) {
        setCreateError(
          err instanceof ApiError ? err.message : "Failed to create bucket",
        );
      } else {
        refresh();
      }
    } finally {
      setBusy(false);
    }
  };

  if (creating) {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <input
            autoFocus
            data-testid={testId ? `${testId}-new` : undefined}
            className={inputClass}
            placeholder="new-bucket-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleCreate();
              }
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => void handleCreate()}
            className="shrink-0 rounded-lg bg-caliber-purple px-3 py-1.5 text-xs font-medium text-white transition hover:bg-caliber-purple-dark disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setCreating(false)}
            className="shrink-0 px-1.5 py-1.5 text-xs font-medium text-zinc-500 hover:text-zinc-800"
          >
            Cancel
          </button>
        </div>
        {createError && (
          <p className="text-[11px] text-red-600">{createError}</p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <select
        data-testid={testId}
        aria-label="Select bucket"
        className={selectClass}
        value={missing ? "" : value}
        onChange={(e) => handleSelect(e.target.value)}
      >
        <option value="">
          {loading ? "Loading buckets…" : "Select a bucket…"}
        </option>
        {buckets.map((b) => (
          <option key={b.name} value={b.name}>
            {b.name}
          </option>
        ))}
        {canManage && (
          <option value={CREATE_SENTINEL}>+ Create new bucket…</option>
        )}
      </select>

      {missing && (
        <div className="flex items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-800">
          <span className="truncate">
            Bucket <span className="font-mono font-medium">{value}</span>{" "}
            doesn&apos;t exist yet.
          </span>
          {canManage && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void createMissing()}
              className="shrink-0 rounded-md bg-amber-600 px-2 py-1 font-medium text-white transition hover:bg-amber-700 disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create it"}
            </button>
          )}
        </div>
      )}
      {error && (
        <p className="text-[11px] text-red-600">
          Failed to load buckets: {error.message}
        </p>
      )}
      {createError && <p className="text-[11px] text-red-600">{createError}</p>}
    </div>
  );
}

interface BucketPrefixFieldProps {
  /** Bucket the prefix lives in (folder creation needs it). */
  bucket: string;
  value: string;
  onChange: (prefix: string) => void;
  placeholder?: string;
  testId?: string;
}

/**
 * Prefix (folder) input with an inline "Create folder" action, so a prefix that
 * doesn't exist yet can be materialized in the bucket without leaving the
 * editor. Nested prefixes (``a/b/c``) are created one segment at a time because
 * the folder API rejects names containing ``/``; an already-existing segment
 * (409) is treated as success.
 */
export function BucketPrefixField({
  bucket,
  value,
  onChange,
  placeholder,
  testId,
}: BucketPrefixFieldProps): JSX.Element {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const canManage = useCanManageStorage();

  const canCreate = bucket.trim() !== "" && value.trim() !== "";

  const createFolder = async (): Promise<void> => {
    setBusy(true);
    setMsg(null);
    try {
      const segments = value
        .replace(/\\/g, "/")
        .split("/")
        .map((s) => s.trim())
        .filter(Boolean);
      let prefix = "";
      for (const segment of segments) {
        try {
          await caliberApi.createObjectStoreFolder(bucket, prefix, segment);
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 409)) throw err;
        }
        prefix = `${prefix}${segment}/`;
      }
      setMsg({ kind: "ok", text: `Folder ${prefix} ready` });
    } catch (err) {
      setMsg({
        kind: "err",
        text: err instanceof ApiError ? err.message : "Failed to create folder",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <input
          data-testid={testId}
          className={inputClass}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        {canManage && (
          <button
            type="button"
            data-testid={testId ? `${testId}-create` : undefined}
            disabled={!canCreate || busy}
            onClick={() => void createFolder()}
            title={
              canCreate
                ? "Create this folder in the bucket"
                : "Pick a bucket and enter a prefix first"
            }
            className="shrink-0 rounded-lg border border-zinc-200 bg-white px-2.5 py-1.5 text-xs font-medium text-zinc-700 transition hover:border-zinc-300 hover:bg-zinc-50 disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create folder"}
          </button>
        )}
      </div>
      {msg && (
        <p
          className={`text-[11px] ${msg.kind === "ok" ? "text-emerald-600" : "text-red-600"}`}
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}

interface BucketContentsFieldProps {
  bucket: string;
  prefix: string;
  /** How many object names to show before collapsing to "…". */
  previewLimit?: number;
  testId?: string;
}

const DEFAULT_PREVIEW_LIMIT = 4;

/**
 * Read-only peek at what an Input Bucket node will actually read: object count,
 * a few names, and existence status — plus an inline uploader (which refreshes
 * the peek on success) and a deep-link into the full Object Store browser.
 *
 * This is the "make state visible and fixable in-panel, delegate heavy
 * management out-of-panel" surface for bucket nodes.
 */
export function BucketContentsField({
  bucket,
  prefix,
  previewLimit = DEFAULT_PREVIEW_LIMIT,
  testId,
}: BucketContentsFieldProps): JSX.Element {
  const trimmedBucket = bucket.trim();
  const normPrefix = (prefix || "").replace(/^\/+/, "");

  const fetcher = useCallback(
    (signal: AbortSignal) => {
      if (trimmedBucket === "") return Promise.resolve(null);
      return caliberApi.listObjectStoreObjects(
        trimmedBucket,
        normPrefix,
        { recursive: true },
        signal,
      );
    },
    [trimmedBucket, normPrefix],
  );
  const { data, loading, error, refresh } = useApi(fetcher, [
    trimmedBucket,
    normPrefix,
  ]);

  // Drop folder markers so the count matches what the runtime actually reads.
  const objects = (data?.objects ?? []).filter((o) => !o.key.endsWith("/"));
  const names = objects
    .slice(0, previewLimit)
    .map((o) =>
      normPrefix && o.key.startsWith(normPrefix)
        ? o.key.slice(normPrefix.length)
        : o.key,
    );
  const extra = objects.length - names.length;
  const truncated = data?.is_truncated ?? false;
  const canManage = useCanManageStorage();

  const browseHref = buildCaliberRouteHref(
    `/object-store?bucket=${encodeURIComponent(trimmedBucket)}${
      normPrefix ? `&prefix=${encodeURIComponent(normPrefix)}` : ""
    }`,
  );

  return (
    <div
      className="space-y-1.5 rounded-lg border border-zinc-200 bg-zinc-50/60 p-2.5"
      data-testid={testId}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-zinc-500">Contents</span>
        {trimmedBucket !== "" && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={refresh}
              className="text-[11px] font-medium text-zinc-500 hover:text-zinc-800"
            >
              Refresh
            </button>
            <a
              href={browseHref}
              target="_blank"
              rel="noreferrer"
              className="text-[11px] font-medium text-caliber-purple hover:underline"
            >
              Open in Object Store ↗
            </a>
          </div>
        )}
      </div>

      {trimmedBucket === "" ? (
        <p className="text-[11px] text-zinc-400">
          Select a bucket to see its contents.
        </p>
      ) : loading ? (
        <p className="text-[11px] text-zinc-400">Checking…</p>
      ) : error ? (
        <p className="text-[11px] text-red-600">{error.message}</p>
      ) : objects.length === 0 ? (
        <p className="text-[11px] text-amber-700">
          No objects under this prefix yet
          {canManage ? " — upload one below." : "."}
        </p>
      ) : (
        <p className="text-[11px] text-zinc-600">
          <span className="font-medium text-emerald-700">
            {objects.length}
            {truncated ? "+" : ""} object
            {objects.length === 1 && !truncated ? "" : "s"}
          </span>
          {names.length > 0 && (
            <span className="text-zinc-500">
              {" · "}
              {names.join(", ")}
              {extra > 0 || truncated ? " …" : ""}
            </span>
          )}
        </p>
      )}

      <BucketUploadField
        bucket={bucket}
        prefix={prefix}
        onUploaded={refresh}
        testId={testId ? `${testId}-upload` : undefined}
      />
    </div>
  );
}

interface BucketUploadFieldProps {
  /** Bucket to upload into. */
  bucket: string;
  /** Key prefix; uploaded objects land at ``{prefix}{filename}``. */
  prefix: string;
  /** Called after a successful upload (e.g. to refresh a listing). */
  onUploaded?: () => void;
  testId?: string;
}

/**
 * One-click file upload that seeds objects into a bucket/prefix without leaving
 * the editor — handy for populating an Input Bucket before a run. Supports
 * multiple files; each lands at ``{prefix}{filename}``.
 */
export function BucketUploadField({
  bucket,
  prefix,
  onUploaded,
  testId,
}: BucketUploadFieldProps): JSX.Element {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const canManage = useCanManageStorage();

  const canUpload = bucket.trim() !== "";

  const handleFiles = async (files: FileList | null): Promise<void> => {
    if (!files || files.length === 0) return;
    setBusy(true);
    setMsg(null);
    try {
      let count = 0;
      for (const file of Array.from(files)) {
        await caliberApi.uploadObjectStoreObject(bucket, file, prefix || "");
        count += 1;
      }
      setMsg({
        kind: "ok",
        text: `Uploaded ${count} object${count === 1 ? "" : "s"}`,
      });
      onUploaded?.();
    } catch (err) {
      setMsg({
        kind: "err",
        text: err instanceof ApiError ? err.message : "Upload failed",
      });
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  // Upload requires the admin scope server-side — hide it entirely otherwise.
  if (!canManage) return <></>;

  return (
    <div className="space-y-1.5">
      <input
        ref={inputRef}
        type="file"
        multiple
        aria-label="Upload objects to bucket"
        className="hidden"
        data-testid={testId ? `${testId}-input` : undefined}
        onChange={(e) => void handleFiles(e.target.files)}
      />
      <button
        type="button"
        data-testid={testId}
        disabled={!canUpload || busy}
        onClick={() => inputRef.current?.click()}
        title={
          canUpload
            ? "Upload file(s) into this bucket/prefix"
            : "Pick a bucket first"
        }
        className="w-full rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-2 text-xs font-medium text-zinc-600 transition hover:border-zinc-400 hover:bg-zinc-50 disabled:opacity-50"
      >
        {busy ? "Uploading…" : "⬆ Upload object(s)"}
      </button>
      {msg && (
        <p
          className={`text-[11px] ${msg.kind === "ok" ? "text-emerald-600" : "text-red-600"}`}
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}
