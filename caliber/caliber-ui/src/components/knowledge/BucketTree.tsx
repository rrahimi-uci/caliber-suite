/**
 * BucketTree — expandable object-store browser used by the Knowledge Base
 * Build stage to pick the folder/file scope of a build.
 *
 * Unlike the old breadcrumb + two-column browser, folders expand/collapse in
 * place (lazy-loading their children on first expand) and every row carries a
 * checkbox. Checking a folder or file toggles it into the parent's
 * ``selectedSources`` list; a top-level "Select all" toggles every entry in the
 * current view at once. This is the single canonical entry point for creating a
 * knowledge base from object-store files (the old "Build KB" launchers on the
 * Object Store page were removed).
 */
import { useMemo, useState } from "react";
import { ChevronRight, FileText, Folder } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { KnowledgeSourceSelection } from "@/api/knowledgeTypes";
import type { ObjectStoreListing } from "@/api/workflowTypes";
import { useApiQuery } from "@/hooks/useApiQuery";

/** Compact byte formatter (mirrors the one used on the Object Store page). */
function humanSize(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exp = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const value = bytes / 1024 ** exp;
  return `${value >= 10 || exp === 0 ? Math.round(value) : value.toFixed(1)} ${units[exp]}`;
}

interface FolderEntry {
  prefix: string;
  label: string;
}

interface FileEntry {
  key: string;
  label: string;
  size: number;
}

/** Split a listing into the folders + files that live directly under `prefix`. */
function levelEntries(
  listing: ObjectStoreListing | null,
  prefix: string,
): { folders: FolderEntry[]; files: FileEntry[] } {
  if (!listing) return { folders: [], files: [] };
  const folders = listing.prefixes
    .map((p) => ({ prefix: p, label: p.slice(prefix.length).replace(/\/$/, "") }))
    .filter((f) => f.label);
  const files = listing.objects
    .map((o) => ({ key: o.key, label: o.key.slice(prefix.length), size: o.size }))
    .filter((f) => f.label && !f.label.includes("/"));
  return { folders, files };
}

interface TreeContext {
  bucket: string;
  isSelected: (sel: KnowledgeSourceSelection) => boolean;
  onToggle: (sel: KnowledgeSourceSelection) => void;
}

interface BucketTreeProps extends TreeContext {
  /** Lowercased filter applied to the top-level entries only. */
  filter?: string;
  /** Replace the whole selection (used by the top-level "Select all"). */
  onBulk: (sources: KnowledgeSourceSelection[], select: boolean) => void;
}

export function BucketTree({
  bucket,
  filter = "",
  isSelected,
  onToggle,
  onBulk,
}: BucketTreeProps): JSX.Element {
  const listingQuery = useApiQuery<ObjectStoreListing | null>(
    ["knowledge-bases", "object-store-tree", bucket, ""],
    (signal) =>
      bucket
        ? caliberApi.listObjectStoreObjects(bucket, "", {}, signal)
        : Promise.resolve(null),
    { enabled: Boolean(bucket) },
  );

  const { folders, files } = useMemo(() => {
    const level = levelEntries(listingQuery.data ?? null, "");
    if (!filter) return level;
    return {
      folders: level.folders.filter((f) =>
        f.label.toLowerCase().includes(filter),
      ),
      files: level.files.filter((f) => f.label.toLowerCase().includes(filter)),
    };
  }, [listingQuery.data, filter]);

  const topLevelSources = useMemo<KnowledgeSourceSelection[]>(
    () => [
      ...folders.map((f) => ({ kind: "folder" as const, path: f.prefix })),
      ...files.map((f) => ({ kind: "file" as const, path: f.key })),
    ],
    [folders, files],
  );
  const allSelected =
    topLevelSources.length > 0 && topLevelSources.every((s) => isSelected(s));

  if (!bucket) {
    return (
      <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-center text-sm text-slate-400">
        Select a bucket to browse its folders and files.
      </div>
    );
  }

  return (
    <div className="rounded-2xl border border-slate-200/70 bg-white">
      <div className="flex items-center justify-between gap-3 border-b border-slate-200/70 px-4 py-2.5">
        <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-slate-300 text-caliber-purple accent-caliber-purple"
            checked={allSelected}
            disabled={topLevelSources.length === 0}
            onChange={() => onBulk(topLevelSources, !allSelected)}
            aria-label="Select all in view"
            data-testid="kb-tree-select-all"
          />
          Select all in view
        </label>
        <span className="text-xs text-slate-400">
          {folders.length} folder{folders.length === 1 ? "" : "s"} ·{" "}
          {files.length} file{files.length === 1 ? "" : "s"}
        </span>
      </div>
      <div
        className="max-h-[28rem] overflow-y-auto p-2"
        data-testid="kb-bucket-tree"
      >
        {listingQuery.isLoading ? (
          <div className="px-3 py-6 text-center text-sm text-slate-400">
            Loading…
          </div>
        ) : folders.length === 0 && files.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-center text-sm text-slate-400">
            No folders or files in this bucket.
          </div>
        ) : (
          <>
            {folders.map((folder) => (
              <FolderRow
                key={folder.prefix}
                folder={folder}
                depth={0}
                bucket={bucket}
                isSelected={isSelected}
                onToggle={onToggle}
              />
            ))}
            {files.map((file) => (
              <FileRow key={file.key} file={file} depth={0} {...{ isSelected, onToggle }} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

interface FolderRowProps extends TreeContext {
  folder: FolderEntry;
  depth: number;
}

function FolderRow({
  folder,
  depth,
  bucket,
  isSelected,
  onToggle,
}: FolderRowProps): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const sel: KnowledgeSourceSelection = { kind: "folder", path: folder.prefix };
  const checked = isSelected(sel);
  return (
    <div>
      <div
        className="flex items-center gap-2 rounded-lg py-1.5 pr-2 hover:bg-slate-50"
        style={{ paddingLeft: depth * 18 + 4 }}
      >
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="grid h-5 w-5 flex-shrink-0 place-items-center rounded text-slate-400 hover:bg-slate-200/70 hover:text-slate-600"
          aria-label={expanded ? "Collapse folder" : "Expand folder"}
          aria-expanded={expanded ? "true" : "false"}
        >
          <ChevronRight
            className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-90" : ""}`}
          />
        </button>
        <input
          type="checkbox"
          className="h-4 w-4 flex-shrink-0 rounded border-slate-300 text-caliber-purple accent-caliber-purple"
          checked={checked}
          onChange={() => onToggle(sel)}
          aria-label={`Select folder ${folder.label}`}
        />
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="flex min-w-0 items-center gap-2 text-left text-sm font-medium text-slate-700"
        >
          <Folder className="h-4 w-4 flex-shrink-0 text-caliber-purple" />
          <span className="truncate">{folder.label}</span>
        </button>
      </div>
      {expanded && (
        <SubLevel
          bucket={bucket}
          prefix={folder.prefix}
          depth={depth + 1}
          isSelected={isSelected}
          onToggle={onToggle}
        />
      )}
    </div>
  );
}

interface FileRowProps extends Pick<TreeContext, "isSelected" | "onToggle"> {
  file: FileEntry;
  depth: number;
}

function FileRow({ file, depth, isSelected, onToggle }: FileRowProps): JSX.Element {
  const sel: KnowledgeSourceSelection = { kind: "file", path: file.key };
  const checked = isSelected(sel);
  return (
    <label
      className="flex cursor-pointer items-center gap-2 rounded-lg py-1.5 pr-2 hover:bg-slate-50"
      style={{ paddingLeft: depth * 18 + 27 }}
    >
      <input
        type="checkbox"
        className="h-4 w-4 flex-shrink-0 rounded border-slate-300 text-caliber-purple accent-caliber-purple"
        checked={checked}
        onChange={() => onToggle(sel)}
        aria-label={`Select file ${file.label}`}
      />
      <FileText className="h-4 w-4 flex-shrink-0 text-slate-400" />
      <span className="min-w-0 flex-1 truncate text-sm text-slate-700">
        {file.label}
      </span>
      <span className="flex-shrink-0 text-xs text-slate-400">
        {humanSize(file.size)}
      </span>
    </label>
  );
}

interface SubLevelProps extends TreeContext {
  prefix: string;
  depth: number;
}

function SubLevel({
  bucket,
  prefix,
  depth,
  isSelected,
  onToggle,
}: SubLevelProps): JSX.Element {
  const listingQuery = useApiQuery<ObjectStoreListing | null>(
    ["knowledge-bases", "object-store-tree", bucket, prefix],
    (signal) => caliberApi.listObjectStoreObjects(bucket, prefix, {}, signal),
    { enabled: Boolean(bucket) },
  );
  const { folders, files } = levelEntries(listingQuery.data ?? null, prefix);

  if (listingQuery.isLoading) {
    return (
      <div
        className="py-1.5 text-xs text-slate-400"
        style={{ paddingLeft: depth * 18 + 27 }}
      >
        Loading…
      </div>
    );
  }
  if (folders.length === 0 && files.length === 0) {
    return (
      <div
        className="py-1.5 text-xs italic text-slate-300"
        style={{ paddingLeft: depth * 18 + 27 }}
      >
        Empty folder.
      </div>
    );
  }
  return (
    <>
      {folders.map((folder) => (
        <FolderRow
          key={folder.prefix}
          folder={folder}
          depth={depth}
          bucket={bucket}
          isSelected={isSelected}
          onToggle={onToggle}
        />
      ))}
      {files.map((file) => (
        <FileRow key={file.key} file={file} depth={depth} {...{ isSelected, onToggle }} />
      ))}
    </>
  );
}
