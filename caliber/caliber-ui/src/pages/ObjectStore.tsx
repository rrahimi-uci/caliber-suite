/**
 * Object Store — a friendly S3 / MinIO file manager.
 *
 * Left: buckets (create / select / delete). Right: a file browser for the
 * selected bucket — breadcrumb navigation, a search box (filter the current
 * folder, or recursively search the whole bucket), multi-select with bulk
 * download + delete, drag-and-drop multi-file upload with progress, "new
 * folder", sortable columns, and per-row download / copy-key / delete. Reads
 * are open to any signed-in user; mutations are admin-gated by the backend.
 *
 * Visuals follow the shared flat `.card` idiom (PageHeader + bordered white
 * panels) rather than a bespoke glassmorphism treatment.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { useSearchParams } from "react-router-dom";
import {
  ArrowDown,
  ArrowUp,
  Check,
  CheckSquare,
  ChevronRight,
  Copy,
  CornerLeftUp,
  Database,
  Download,
  Eye,
  ExternalLink,
  File as FileIcon,
  FileArchive,
  FileAudio,
  FileCode2,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileType2,
  FileVideo,
  Folder,
  FolderPlus,
  GripVertical,
  HardDrive,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  Upload,
  UploadCloud,
  X,
  type LucideIcon,
} from "lucide-react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import { ClearFiltersButton } from "@/components/ClearFiltersButton";
import { PageHeader } from "@/components/PageHeader";
import { SearchInput } from "@/components/SearchInput";
import { FilterSelect } from "@/components/FilterSelect";
import {
  DataTable,
  MarkdownView,
  parseDelimited,
} from "@/components/objectstore/previewRenderers";
import type {
  ObjectStoreExtract,
  ObjectStoreListing,
  ObjectStoreObject,
  ObjectStorePreview,
} from "@/api/workflowTypes";
import { useApiQuery, useInvalidate } from "@/hooks/useApiQuery";

type SortKey = "name" | "size" | "created" | "modified";
type SortDir = "asc" | "desc";

// File icon buckets keyed off extension so the row icon matches the object.
const FILE_ICON_EXTENSIONS = {
  image: [
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
    "bmp",
    "ico",
    "tif",
    "tiff",
    "avif",
    "heic",
    "heif",
  ],
  code: [
    "json",
    "jsonl",
    "js",
    "jsx",
    "ts",
    "tsx",
    "py",
    "html",
    "css",
    "yaml",
    "yml",
    "xml",
    "sh",
    "sql",
  ],
  text: ["txt", "md", "markdown", "log", "csv", "tsv"],
  archive: ["zip", "tar", "gz", "tgz", "rar", "7z", "bz2"],
  audio: ["mp3", "wav", "ogg", "oga", "opus", "m4a", "aac", "flac", "weba"],
  video: ["mp4", "m4v", "webm", "ogv", "mov", "mkv"],
  // Office documents previewable via server-side extraction.
  document: ["doc", "docx", "ppt", "pptx", "xls", "xlsx", "xlsm"],
};

function extOf(name: string): string {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

// "Search all folders" fetches recursively in pages; cap the fan-out so a huge
// bucket can't hang the browser. The UI flags the result as truncated.
const SEARCH_ALL_PAGE_CAP = 20;
const BUCKET_PANE_WIDTH_KEY = "caliber.objectStore.bucketPaneWidth";
const BUCKET_PANE_DEFAULT = 300;
const BUCKET_PANE_MIN = 240;
const OBJECT_BROWSER_MIN = 640;

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function readPaneWidth(key: string, fallback: number): number {
  if (typeof window === "undefined") return fallback;
  const stored = Number(window.localStorage.getItem(key));
  return Number.isFinite(stored) && stored > 0 ? stored : fallback;
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${MONTHS[d.getMonth()]} ${pad(d.getDate())}, ${d.getFullYear()} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fileIconFor(name: string): LucideIcon {
  const ext = extOf(name);
  if (FILE_ICON_EXTENSIONS.image.includes(ext)) return FileImage;
  if (FILE_ICON_EXTENSIONS.audio.includes(ext)) return FileAudio;
  if (FILE_ICON_EXTENSIONS.video.includes(ext)) return FileVideo;
  if (ext === "xls" || ext === "xlsx" || ext === "xlsm")
    return FileSpreadsheet;
  if (FILE_ICON_EXTENSIONS.document.includes(ext)) return FileType2;
  if (FILE_ICON_EXTENSIONS.code.includes(ext)) return FileCode2;
  if (FILE_ICON_EXTENSIONS.text.includes(ext)) return FileText;
  if (FILE_ICON_EXTENSIONS.archive.includes(ext)) return FileArchive;
  return FileIcon;
}

// Same buckets as the row icon, collapsed to a single type key used by the
// "Type" filter. Anything not in the icon map falls into "other".
type ObjectFileType =
  | "image"
  | "code"
  | "text"
  | "archive"
  | "audio"
  | "video"
  | "document"
  | "other";

function fileTypeOf(name: string): ObjectFileType {
  const ext = extOf(name);
  if (FILE_ICON_EXTENSIONS.image.includes(ext)) return "image";
  if (FILE_ICON_EXTENSIONS.audio.includes(ext)) return "audio";
  if (FILE_ICON_EXTENSIONS.video.includes(ext)) return "video";
  if (FILE_ICON_EXTENSIONS.document.includes(ext)) return "document";
  if (FILE_ICON_EXTENSIONS.code.includes(ext)) return "code";
  if (FILE_ICON_EXTENSIONS.text.includes(ext)) return "text";
  if (FILE_ICON_EXTENSIONS.archive.includes(ext)) return "archive";
  return "other";
}

// Preview render strategy for the modal. Drives which branch renders the
// content (and whether we hit the server-side extraction endpoint).
type PreviewKind =
  | "markdown"
  | "text"
  | "image"
  | "pdf"
  | "audio"
  | "video"
  | "csv"
  | "office"
  | "none";

function previewKindFor(name: string, contentType: string): PreviewKind {
  const ext = extOf(name);
  const ct = contentType.toLowerCase();
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "csv" || ext === "tsv") return "csv";
  if (ct.startsWith("image/") || FILE_ICON_EXTENSIONS.image.includes(ext))
    return "image";
  if (ct === "application/pdf" || ext === "pdf") return "pdf";
  if (ct.startsWith("audio/") || FILE_ICON_EXTENSIONS.audio.includes(ext))
    return "audio";
  if (ct.startsWith("video/") || FILE_ICON_EXTENSIONS.video.includes(ext))
    return "video";
  if (FILE_ICON_EXTENSIONS.document.includes(ext)) return "office";
  return "none";
}

function compareIsoDates(
  left: string | null | undefined,
  right: string | null | undefined,
): number {
  const leftTime = left ? Date.parse(left) : Number.NaN;
  const rightTime = right ? Date.parse(right) : Number.NaN;
  if (Number.isNaN(leftTime) && Number.isNaN(rightTime)) return 0;
  if (Number.isNaN(leftTime)) return -1;
  if (Number.isNaN(rightTime)) return 1;
  return leftTime - rightTime;
}

function SortableTh({
  label,
  sortKey,
  current,
  dir,
  onSort,
  align = "left",
  widthClass = "",
}: {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "left" | "right";
  widthClass?: string;
}): JSX.Element {
  const active = current === sortKey;
  return (
    <th
      className={`px-4 py-2.5 font-bold ${widthClass} ${align === "right" ? "text-right" : "text-left"}`}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={`inline-flex items-center gap-1 transition hover:text-slate-700 ${
          active ? "text-slate-800" : ""
        }`}
      >
        {label}
        {active &&
          (dir === "asc" ? (
            <ArrowUp className="h-3 w-3" />
          ) : (
            <ArrowDown className="h-3 w-3" />
          ))}
      </button>
    </th>
  );
}

export function ObjectStore(): JSX.Element {
  const invalidate = useInvalidate();
  const fileInput = useRef<HTMLInputElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const browserGridRef = useRef<HTMLDivElement>(null);

  // Deep-link support:
  // - `/object-store?bucket=X&prefix=Y/` opens a folder
  // - `/object-store?bucket=X&key=Y/file.jsonl` opens a specific object
  const [searchParams, setSearchParams] = useSearchParams();
  const initialBucket = searchParams.get("bucket");
  const initialPrefix = searchParams.get("prefix") ?? "";
  const initialKey = searchParams.get("key");

  const [selectedBucket, setSelectedBucket] = useState<string | null>(
    initialBucket,
  );
  const [prefix, setPrefix] = useState(initialPrefix);
  const [selectedKey, setSelectedKey] = useState<string | null>(initialKey);
  const [newBucket, setNewBucket] = useState("");
  const [bucketFilter, setBucketFilter] = useState("");
  const [newFolderName, setNewFolderName] = useState<string | null>(null); // null = input hidden
  const [search, setSearch] = useState("");
  // Type filter over the listed rows. ``""`` = All; ``folder`` shows only
  // folders; the remaining keys (image/code/text/archive/other) match files by
  // computed type. Additive with the text search.
  const [typeFilter, setTypeFilter] = useState("");
  const [searchAll, setSearchAll] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadState, setUploadState] = useState<{
    name: string;
    done: number;
    total: number;
  } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [bucketPaneWidth, setBucketPaneWidth] = useState(() =>
    readPaneWidth(BUCKET_PANE_WIDTH_KEY, BUCKET_PANE_DEFAULT),
  );

  const statusQuery = useApiQuery(["object-store", "status"], (s) =>
    caliberApi.getObjectStoreStatus(s),
  );
  const bucketsQuery = useApiQuery(["object-store", "buckets"], (s) =>
    caliberApi.listObjectStoreBuckets(s),
  );

  // Open the first bucket automatically so the file browser (search, upload,
  // folders) is visible immediately instead of a bare "select a bucket" prompt.
  useEffect(() => {
    if (selectedBucket) return;
    const first = bucketsQuery.data?.[0];
    if (first) setSelectedBucket(first.name);
  }, [bucketsQuery.data, selectedBucket]);

  // If the URL deep-links directly to an object key, derive its parent folder
  // unless a prefix was explicitly provided.
  useEffect(() => {
    if (!selectedKey || prefix || initialPrefix) return;
    const i = selectedKey.lastIndexOf("/");
    setPrefix(i >= 0 ? selectedKey.slice(0, i + 1) : "");
  }, [initialPrefix, prefix, selectedKey]);

  // Keep the URL in sync so users can copy/share direct object links.
  useEffect(() => {
    const next = new URLSearchParams();
    if (selectedBucket) next.set("bucket", selectedBucket);
    if (prefix) next.set("prefix", prefix);
    if (selectedKey) next.set("key", selectedKey);
    setSearchParams(next, { replace: true });
  }, [prefix, selectedBucket, selectedKey, setSearchParams]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(
      BUCKET_PANE_WIDTH_KEY,
      String(Math.round(bucketPaneWidth)),
    );
  }, [bucketPaneWidth]);

  useEffect(() => {
    if (!previewKey) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setPreviewKey(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [previewKey]);

  const startBucketResize = useCallback(
    (event: React.PointerEvent<HTMLButtonElement>): void => {
      const bounds = layoutRef.current?.getBoundingClientRect();
      if (!bounds) return;
      event.preventDefault();

      const max = Math.min(
        520,
        Math.max(BUCKET_PANE_MIN, bounds.width - OBJECT_BROWSER_MIN),
      );
      const previousCursor = document.body.style.cursor;
      const previousSelect = document.body.style.userSelect;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const move = (moveEvent: PointerEvent): void => {
        setBucketPaneWidth(
          clampNumber(moveEvent.clientX - bounds.left, BUCKET_PANE_MIN, max),
        );
      };
      const stop = (): void => {
        document.body.style.cursor = previousCursor;
        document.body.style.userSelect = previousSelect;
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
      };

      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
      move(event.nativeEvent);
    },
    [],
  );


  const objectsKey = [
    "object-store",
    "objects",
    selectedBucket ?? "",
    searchAll ? "::all" : prefix,
  ];
  const objectsQuery = useApiQuery(
    objectsKey,
    async (s): Promise<ObjectStoreListing | null> => {
      if (!selectedBucket) return null;
      if (searchAll) {
        const all: ObjectStoreObject[] = [];
        let token: string | undefined;
        let truncated = false;
        for (let page = 0; page < SEARCH_ALL_PAGE_CAP; page += 1) {
          const r = await caliberApi.listObjectStoreObjects(
            selectedBucket,
            "",
            { recursive: true, token },
            s,
          );
          all.push(...r.objects);
          if (!r.next_token) break;
          token = r.next_token;
          if (page === SEARCH_ALL_PAGE_CAP - 1) truncated = true;
        }
        return {
          bucket: selectedBucket,
          prefix: "",
          prefixes: [],
          objects: all,
          next_token: null,
          is_truncated: truncated,
        };
      }
      return caliberApi.listObjectStoreObjects(selectedBucket, prefix, {}, s);
    },
    { enabled: Boolean(selectedBucket) },
  );
  const previewQuery = useApiQuery<ObjectStorePreview>(
    ["object-store", "preview", selectedBucket ?? "", previewKey ?? ""],
    (signal) =>
      caliberApi.getObjectStoreObjectPreview(
        selectedBucket!,
        previewKey!,
        512 * 1024,
        signal,
      ),
    { enabled: Boolean(selectedBucket && previewKey) },
  );
  // Office documents (Word/PowerPoint/Excel) are extracted server-side so we can
  // show their text/tables inline. Only fired when the previewed key is one.
  const previewIsOffice = Boolean(
    previewKey && FILE_ICON_EXTENSIONS.document.includes(extOf(previewKey)),
  );
  const extractQuery = useApiQuery<ObjectStoreExtract>(
    ["object-store", "extract", selectedBucket ?? "", previewKey ?? ""],
    (signal) =>
      caliberApi.getObjectStoreObjectExtract(
        selectedBucket!,
        previewKey!,
        signal,
      ),
    { enabled: Boolean(selectedBucket && previewKey && previewIsOffice) },
  );

  const status = statusQuery.data;
  const listing = objectsQuery.data ?? null;
  const q = search.trim().toLowerCase();
  const hasObjectBrowserFilters = Boolean(search || typeFilter || searchAll);

  const folders = useMemo(() => {
    // A non-folder type filter hides every folder (folders have no file type).
    if (!listing || searchAll || (typeFilter && typeFilter !== "folder")) {
      return [];
    }
    return listing.prefixes
      .map((p) => ({
        prefix: p,
        label: p.slice(prefix.length).replace(/\/$/, ""),
      }))
      .filter((f) => !q || f.label.toLowerCase().includes(q))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [listing, searchAll, prefix, q, typeFilter]);

  const files = useMemo(() => {
    // The "folder" type filter hides every file.
    if (!listing || typeFilter === "folder") return [];
    const rows = listing.objects.map((o) => ({
      ...o,
      label: searchAll ? o.key : o.key.slice(prefix.length),
    }));
    const typed = typeFilter
      ? rows.filter((r) => fileTypeOf(r.label) === typeFilter)
      : rows;
    const searched = q
      ? typed.filter((r) => r.label.toLowerCase().includes(q))
      : typed;
    const sign = sortDir === "asc" ? 1 : -1;
    return [...searched].sort((a, b) => {
      if (sortKey === "size") return (a.size - b.size) * sign;
      if (sortKey === "created") {
        return (
          compareIsoDates(
            a.created_at ?? a.last_modified,
            b.created_at ?? b.last_modified,
          ) * sign
        );
      }
      if (sortKey === "modified")
        return compareIsoDates(a.last_modified, b.last_modified) * sign;
      return a.label.localeCompare(b.label) * sign;
    });
  }, [listing, searchAll, prefix, q, sortKey, sortDir, typeFilter]);

  const visibleKeys = useMemo(() => files.map((f) => f.key), [files]);
  const allSelected =
    visibleKeys.length > 0 && visibleKeys.every((k) => selected.has(k));
  const someSelected = !allSelected && visibleKeys.some((k) => selected.has(k));

  const clearSelection = (): void => setSelected(new Set());

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>, ...keys: unknown[][]) => {
      setBusy(label);
      setError(null);
      try {
        await fn();
        await Promise.all(keys.map((k) => invalidate(k)));
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [invalidate],
  );

  const navigate = (next: string): void => {
    setPrefix(next);
    setSearchAll(false);
    setSelectedKey(null);
    setPreviewKey(null);
    setSearch("");
    setTypeFilter("");
    setNewFolderName(null);
    clearSelection();
    setError(null);
  };

  const openBucket = (bucket: string): void => {
    setSelectedBucket(bucket);
    navigate("");
  };

  // Open the object's content in a new browser tab (served inline by the
  // backend): PDFs / text / markdown / JSON / images render in the tab; other
  // types (e.g. Office docs the browser can't render) save instead.
  const openObject = (key: string): void => {
    if (!selectedBucket) return;
    setSelectedKey(key);
    window.open(
      caliberApi.objectStoreViewUrl(selectedBucket, key),
      "_blank",
      "noopener,noreferrer",
    );
  };
  const openPreview = (key: string): void => {
    if (!selectedBucket) return;
    setSelectedKey(key);
    setPreviewKey(key);
  };
  const closePreview = (): void => setPreviewKey(null);

  // ---- bucket actions ----
  const createBucket = (): void => {
    const name = newBucket.trim();
    if (!name) return;
    void run(
      "create-bucket",
      async () => {
        await caliberApi.createObjectStoreBucket(name);
        setNewBucket("");
        openBucket(name);
      },
      ["object-store", "buckets"],
      ["object-store", "status"],
    );
  };

  const deleteBucket = (bucket: string): void => {
    if (!window.confirm(`Delete bucket "${bucket}"? It must be empty.`)) return;
    void run(
      `bucket:${bucket}`,
      async () => {
        await caliberApi.deleteObjectStoreBucket(bucket);
        if (selectedBucket === bucket) {
          setSelectedBucket(null);
          navigate("");
        }
      },
      ["object-store", "buckets"],
      ["object-store", "status"],
    );
  };

  // ---- object actions ----
  const uploadFiles = async (fileList: FileList | File[]): Promise<void> => {
    const list = Array.from(fileList);
    if (!list.length || !selectedBucket) return;
    setError(null);
    const errs: string[] = [];
    let done = 0;
    for (const file of list) {
      setUploadState({ name: file.name, done, total: list.length });
      try {
        await caliberApi.uploadObjectStoreObject(
          selectedBucket,
          file,
          searchAll ? "" : prefix,
        );
      } catch (e) {
        errs.push(
          `${file.name}: ${e instanceof ApiError ? e.message : String(e)}`,
        );
      }
      done += 1;
    }
    setUploadState(null);
    if (errs.length)
      setError(`${errs.length} upload(s) failed — ${errs.join("; ")}`);
    await invalidate(objectsKey);
  };

  const onPickFiles = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (picked.length) void uploadFiles(picked);
  };

  const createFolder = (): void => {
    const name = (newFolderName ?? "").trim();
    if (!name || !selectedBucket) {
      setNewFolderName(null);
      return;
    }
    void run(
      "new-folder",
      async () => {
        await caliberApi.createObjectStoreFolder(selectedBucket, prefix, name);
        setNewFolderName(null);
      },
      objectsKey,
    );
  };

  const deleteOne = (key: string): void => {
    if (!selectedBucket || !window.confirm(`Delete "${key.split("/").pop()}"?`))
      return;
    void run(
      `del:${key}`,
      async () => {
        await caliberApi.deleteObjectStoreObjects(selectedBucket, [key]);
        setSelectedKey((current) => (current === key ? null : current));
        setPreviewKey((current) => (current === key ? null : current));
        setSelected((s) => {
          const n = new Set(s);
          n.delete(key);
          return n;
        });
      },
      objectsKey,
    );
  };

  const deleteSelected = (): void => {
    const keys = [...selected];
    if (!selectedBucket || !keys.length) return;
    if (
      !window.confirm(
        `Delete ${keys.length} selected object(s)? This cannot be undone.`,
      )
    )
      return;
    void run(
      "bulk-delete",
      async () => {
        const res = await caliberApi.deleteObjectStoreObjects(
          selectedBucket,
          keys,
        );
        clearSelection();
        setSelectedKey((current) =>
          current && keys.includes(current) ? null : current,
        );
        setPreviewKey((current) =>
          current && keys.includes(current) ? null : current,
        );
        if (res.errors.length)
          setError(`Deleted ${res.deleted}; ${res.errors.length} failed.`);
      },
      objectsKey,
    );
  };

  const deleteFolder = (folderPrefix: string): void => {
    if (!selectedBucket) return;
    if (
      !window.confirm(
        `Delete folder "${folderPrefix}" and ALL its contents? This cannot be undone.`,
      )
    )
      return;
    void run(
      `folder:${folderPrefix}`,
      async () => {
        const res = await caliberApi.deleteObjectStoreFolder(
          selectedBucket,
          folderPrefix,
        );
        setSelectedKey((current) =>
          current && current.startsWith(folderPrefix) ? null : current,
        );
        setPreviewKey((current) =>
          current && current.startsWith(folderPrefix) ? null : current,
        );
        if (res.errors.length)
          setError(`Deleted ${res.deleted}; ${res.errors.length} failed.`);
      },
      objectsKey,
    );
  };

  const downloadSelected = (): void => {
    if (!selectedBucket) return;
    for (const key of selected) {
      const a = document.createElement("a");
      a.href = caliberApi.objectStoreDownloadUrl(selectedBucket, key);
      a.download = key.split("/").pop() ?? "object";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  };

  const copyKey = (key: string): void => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(key).then(() => {
      setCopiedKey(key);
      window.setTimeout(
        () => setCopiedKey((c) => (c === key ? null : c)),
        1500,
      );
    });
  };

  // Copy the keys of all currently-selected objects, one per line.
  const copySelectedKeys = (): void => {
    if (!navigator.clipboard || !selected.size) return;
    void navigator.clipboard.writeText([...selected].join("\n"));
  };

  const toggleSelect = (key: string): void =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });

  const toggleSelectAll = (): void =>
    setSelected((s) => {
      const n = new Set(s);
      if (allSelected) visibleKeys.forEach((k) => n.delete(k));
      else visibleKeys.forEach((k) => n.add(k));
      return n;
    });

  const setSort = (key: SortKey): void => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  const onDrop = (e: React.DragEvent): void => {
    e.preventDefault();
    setDragOver(false);
    if (selectedBucket && e.dataTransfer.files?.length)
      void uploadFiles(e.dataTransfer.files);
  };

  const segments = prefix.split("/").filter(Boolean);
  const bucketCount = bucketsQuery.data?.length ?? 0;
  const folderCount = folders.length;
  const fileCount = files.length;
  const objectCount = folderCount + fileCount;
  const currentPathLabel = searchAll ? "All folders" : prefix || "/";
  const inViewSize = useMemo(
    () => humanSize(files.reduce((sum, f) => sum + f.size, 0)),
    [files],
  );
  const filteredBuckets = useMemo(() => {
    const bf = bucketFilter.trim().toLowerCase();
    const all = bucketsQuery.data ?? [];
    return bf ? all.filter((b) => b.name.toLowerCase().includes(bf)) : all;
  }, [bucketsQuery.data, bucketFilter]);

  const uploadPct =
    uploadState && uploadState.total > 0
      ? Math.round((uploadState.done / uploadState.total) * 100)
      : 0;
  const previewData = previewQuery.data ?? null;
  const previewViewUrl =
    selectedBucket && previewKey
      ? caliberApi.objectStoreViewUrl(selectedBucket, previewKey)
      : null;
  const previewDownloadUrl =
    selectedBucket && previewKey
      ? caliberApi.objectStoreDownloadUrl(selectedBucket, previewKey)
      : null;
  const previewKind: PreviewKind = previewKey
    ? previewKindFor(previewKey, previewData?.content_type ?? "")
    : "none";

  const labelClass =
    "text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500";
  const headerClass =
    "object-store-panel-header border-b border-slate-200/60 bg-gradient-to-r from-slate-50 via-white to-slate-50/80 px-5 py-4";
  const iconButtonClass =
    "inline-flex h-8 w-8 items-center justify-center rounded-xl text-slate-400 transition hover:bg-slate-100 hover:text-slate-600";
  const checkboxClass =
    "h-4 w-4 rounded border-slate-300 text-caliber-purple accent-caliber-purple focus:ring-caliber-500/30";
  const layoutStyle = {
    "--object-store-bucket-width": `${bucketPaneWidth}px`,
  } as CSSProperties;
  const bucketFilterActive = bucketFilter.trim().length > 0;
  const bucketSummary = bucketFilterActive
    ? `${filteredBuckets.length} of ${bucketCount} buckets match the current filter.`
    : `${bucketCount} bucket${bucketCount === 1 ? "" : "s"} available to browse.`;

  return (
    <div
      className="caliber-object-store-page min-w-0 space-y-6"
      data-testid="object-store-page"
    >
      <PageHeader
        title="Object Store"
        crumbs={[
          { label: "Dashboard", to: "/" },
          { label: "Object Store" },
        ]}
        subtitle={
          <>
            Browse workspace buckets, inspect stored artifacts, and manage
            uploads from one place. Reads stay open to signed-in users;
            mutations remain admin-gated.
          </>
        }
      />

      {error && (
        <div className="flex items-start justify-between gap-3 rounded-2xl border border-red-200 bg-red-50/95 px-4 py-3 text-sm text-red-700 shadow-card">
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss error"
            className="text-red-400 hover:text-red-600"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}
      {status && !status.connected && (
        <div
          className="rounded-2xl border border-amber-200 bg-amber-50/95 px-4 py-3 text-sm text-amber-800 shadow-card"
          data-testid="object-store-offline"
        >
          Object store unreachable at{" "}
          <span className="font-mono">{status.endpoint}</span>
          {status.error ? ` — ${status.error}` : ""}. Start MinIO to manage
          buckets.
        </div>
      )}

      <div
        ref={layoutRef}
        className="object-store-resizable-shell grid grid-cols-1 gap-5"
        style={layoutStyle}
      >
        {/* ── Buckets ── */}
        <div className="relative min-h-0 min-w-0">
          <div className="card flex h-full flex-col overflow-hidden border-slate-200/70 bg-white/95 dark:bg-slate-950/90">
            <div className={headerClass}>
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                  <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-caliber-purple shadow-sm dark:bg-violet-500/10 dark:text-violet-200">
                    <Database className="h-5 w-5" />
                  </span>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className={labelClass}>Workspaces</span>
                      <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-[11px] font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-200">
                        {bucketCount}
                      </span>
                    </div>
                    <p className="mt-1 text-sm font-semibold text-slate-800">
                      Bucket browser
                    </p>
                  </div>
                </div>
              </div>
              <p className="mt-3 text-[12px] leading-5 text-slate-500">
                Choose a bucket to browse files or create a new shared
                namespace for artifacts.
              </p>
            </div>

            {/* bucket filter */}
            <div className="border-b border-slate-200/60 px-4 py-3">
              <SearchInput
                value={bucketFilter}
                onChange={setBucketFilter}
                ariaLabel="Filter buckets"
                placeholder="Filter buckets…"
                className="w-full"
              />
              <p className="mt-2 px-0.5 text-[11px] text-slate-400">
                {bucketSummary}
              </p>
            </div>

            <ul className="min-h-0 flex-1 overflow-y-auto p-3">
              {bucketsQuery.isLoading && (
                <li className="rounded-2xl border border-slate-200/70 bg-slate-50/70 px-4 py-5 text-sm text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/70">
                  Loading…
                </li>
              )}
              {bucketsQuery.data?.length === 0 && (
                <li className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-5 py-8 text-center dark:border-slate-700/70">
                  <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-2xl bg-white text-slate-400 shadow-card dark:bg-slate-950 dark:text-slate-200">
                    <Database className="h-5 w-5" />
                  </div>
                  <div className="text-sm font-semibold text-slate-600">
                    No buckets yet.
                  </div>
                  <div className="mt-1 text-xs text-slate-400">
                    Create one below to start organizing artifacts.
                  </div>
                </li>
              )}
              {filteredBuckets.map((b) => {
                const active = selectedBucket === b.name;
                return (
                  <li key={b.name} className="group">
                    <div
                      className={`mt-1.5 flex items-start gap-2.5 rounded-2xl border px-3 py-3 transition ${
                        active
                          ? "object-store-bucket-active border-caliber-200/70 bg-caliber-50 shadow-card ring-1 ring-inset ring-caliber-200/50"
                          : "object-store-bucket-row border-transparent hover:border-slate-200/70 hover:bg-slate-50"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => openBucket(b.name)}
                        className="flex min-w-0 flex-1 items-start gap-2.5 text-left"
                        data-testid={`bucket-${b.name}`}
                        aria-label={b.name}
                        title={b.name}
                      >
                        <span
                          className={`mt-0.5 grid h-7 w-7 flex-shrink-0 place-items-center rounded-lg shadow-card transition ${
                            active
                              ? "bg-caliber-purple text-white"
                              : "bg-slate-100 text-slate-500 group-hover:bg-blue-50 group-hover:text-blue-500"
                          }`}
                        >
                          <HardDrive className="h-3.5 w-3.5" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span
                            className={`block truncate text-sm ${
                              active
                                ? "object-store-bucket-active-name font-bold text-caliber-700"
                                : "object-store-file-name font-semibold text-slate-700"
                            }`}
                          >
                            {b.name}
                          </span>
                          <span className="mt-1 block text-[11px] text-slate-400">
                            {active ? "Active workspace" : "Open bucket"}
                          </span>
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => deleteBucket(b.name)}
                        title="Delete bucket"
                        className="mt-0.5 grid h-6 w-6 flex-shrink-0 place-items-center rounded-md text-slate-300 opacity-0 transition hover:bg-red-50 hover:text-red-600 group-hover:opacity-100"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>

            {/* new bucket */}
            <div className="border-t border-slate-200/60 bg-slate-50/50 p-3 dark:border-slate-700/70 dark:bg-slate-900/80">
              <div className="flex gap-2">
                <input
                  value={newBucket}
                  onChange={(e) => setNewBucket(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && createBucket()}
                  placeholder="new-bucket-name"
                  className="form-input flex-1 !py-1.5 text-[13px]"
                  data-testid="new-bucket-input"
                />
                <button
                  type="button"
                  onClick={createBucket}
                  disabled={!newBucket.trim() || busy === "create-bucket"}
                  title="Create bucket"
                  className="grid h-9 w-9 flex-shrink-0 place-items-center rounded-xl bg-gradient-to-br from-caliber-purple to-blue-500 text-white shadow-card transition hover:brightness-110 disabled:pointer-events-none disabled:opacity-50"
                >
                  <Plus className="h-4 w-4" />
                </button>
              </div>
              <p className="mt-2 px-0.5 text-[10px] leading-4 text-slate-400 dark:text-slate-300">
                3–63 chars, lowercase, validated against the S3 naming regex.
                Bucket creation &amp; deletion require{" "}
                <span className="font-semibold text-slate-500 dark:text-slate-200">admin</span>{" "}
                scope.
              </p>
            </div>
          </div>
          <button
            type="button"
            aria-label="Resize bucket panel"
            title="Drag to resize buckets. Double-click to reset."
            onPointerDown={startBucketResize}
            onDoubleClick={() => setBucketPaneWidth(BUCKET_PANE_DEFAULT)}
            className="object-store-resizer group absolute -right-4 bottom-6 top-6 z-20 hidden w-8 cursor-col-resize items-center justify-center lg:flex"
          >
            <span className="flex h-16 w-3 items-center justify-center rounded-full border border-slate-200/80 bg-white text-slate-400 shadow-sm transition group-hover:h-24 group-hover:border-caliber-300 group-hover:bg-caliber-50 group-hover:text-caliber-600">
              <GripVertical className="h-4 w-4" />
            </span>
          </button>
        </div>

        {/* ── Objects ── */}
        <div className="card min-w-0 overflow-hidden border-slate-200/70 bg-white/95 dark:bg-slate-950/90">
          {!selectedBucket ? (
            <div className="flex min-h-[64vh] items-center justify-center px-6 py-16">
              <div className="max-w-md rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center dark:border-slate-700/70">
                <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-white text-caliber-purple shadow-card dark:bg-slate-950 dark:text-violet-200">
                  <HardDrive className="h-6 w-6" />
                </div>
                <div className="text-sm font-semibold text-slate-600">
                  Select a workspace to browse files.
                </div>
                <div className="mt-1.5 text-xs leading-relaxed text-slate-400">
                  Uploads, folder creation, preview, and bulk actions become
                  available as soon as a bucket is selected.
                </div>
              </div>
            </div>
          ) : (
            <div
              ref={browserGridRef}
              className="flex min-h-[66vh] flex-col xl:h-[calc(100vh-24rem)] xl:min-h-[660px]"
            >
              <div
                className="relative flex min-h-0 min-w-0 flex-1 flex-col"
                data-testid="object-drop-zone"
                onDragOver={(e) => {
                  e.preventDefault();
                  if (selectedBucket) setDragOver(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                }}
                onDrop={onDrop}
              >
                {/* Toolbar: breadcrumb + actions */}
                <div className={headerClass}>
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className={labelClass}>Current location</div>
                      <p className="mt-1 text-[12px] text-slate-400">
                        {objectCount} visible item
                        {objectCount === 1 ? "" : "s"} in{" "}
                        <span className="font-mono text-slate-500">
                          {currentPathLabel}
                        </span>
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-[11px] font-semibold">
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-slate-500 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        {searchAll ? "Whole-bucket search" : "Folder scope"}
                      </span>
                      <span className="rounded-full border border-slate-200/70 bg-white/85 px-3 py-1 text-slate-500 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        {selected.size} selected
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <nav
                      className="flex min-w-0 flex-wrap items-center gap-1 text-sm"
                      aria-label="Breadcrumb"
                    >
                      {searchAll ? (
                        <span className="rounded-full border border-caliber-200/70 bg-caliber-50 px-3 py-1.5 font-semibold text-caliber-700">
                          Search results in {selectedBucket}
                        </span>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="inline-flex items-center gap-1.5 rounded-md bg-caliber-50 px-2 py-1 font-bold text-caliber-purple transition hover:bg-caliber-100"
                            onClick={() => navigate("")}
                            title="Jump to bucket root"
                          >
                            <HardDrive className="h-3.5 w-3.5" />
                            {selectedBucket}
                          </button>
                          {segments.map((seg, i) => {
                            const target = `${segments.slice(0, i + 1).join("/")}/`;
                            return (
                              <span
                                key={target}
                                className="flex items-center gap-1"
                              >
                                <ChevronRight className="h-3.5 w-3.5 text-slate-300" />
                                <button
                                  type="button"
                                  className="rounded-md px-1.5 py-1 font-medium text-slate-600 transition hover:bg-slate-100 hover:text-slate-900"
                                  onClick={() => navigate(target)}
                                >
                                  {seg}
                                </button>
                              </span>
                            );
                          })}
                        </>
                      )}
                    </nav>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() =>
                          setNewFolderName((v) => (v === null ? "" : null))
                        }
                        disabled={searchAll}
                        className="btn-ghost !px-2.5 !py-1.5 disabled:pointer-events-none disabled:opacity-50"
                      >
                        <FolderPlus className="h-4 w-4" /> New folder
                      </button>
                      <input
                        ref={fileInput}
                        type="file"
                        hidden
                        multiple
                        onChange={onPickFiles}
                        data-testid="upload-input"
                      />
                      <button
                        type="button"
                        onClick={() => fileInput.current?.click()}
                        disabled={!status?.connected || uploadState !== null}
                        className="btn-ghost !px-2.5 !py-1.5 disabled:pointer-events-none disabled:opacity-50"
                      >
                        <Upload className="h-4 w-4" />
                        {uploadState
                          ? `Uploading ${uploadState.done + 1}/${uploadState.total}…`
                          : "Upload"}
                      </button>
                      <button
                        type="button"
                        onClick={toggleSelectAll}
                        disabled={visibleKeys.length === 0}
                        className="btn-ghost !px-2.5 !py-1.5 disabled:pointer-events-none disabled:opacity-50"
                      >
                        <CheckSquare className="h-4 w-4" />
                        {allSelected ? "Clear file selection" : "Select all files"}
                      </button>
                      <button
                        type="button"
                        onClick={deleteSelected}
                        disabled={selected.size === 0 || busy === "bulk-delete"}
                        className="btn-ghost !px-2.5 !py-1.5 text-red-600 hover:border-red-200 hover:bg-red-50 hover:text-red-700 disabled:pointer-events-none disabled:opacity-50"
                      >
                        <Trash2 className="h-4 w-4" />
                        Delete selected files
                      </button>
                      <button
                        type="button"
                        onClick={() => void invalidate(objectsKey)}
                        title="Refresh"
                        className={iconButtonClass}
                      >
                        <RefreshCw
                          className={`h-4 w-4 ${objectsQuery.isFetching ? "animate-spin" : ""}`}
                        />
                      </button>
                    </div>
                  </div>
                </div>

                {/* Search row — aligned to the shared FilterBar look (flex row,
                    grow search slot, no card chrome since it sits inside the
                    browser shell; see FilterBar.tsx). */}
                <div className="border-b border-slate-200/60 px-4 py-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="relative min-w-[280px] flex-1 sm:min-w-[360px]">
                      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                      <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={
                          searchAll
                            ? "Search the whole bucket…"
                            : "Filter this folder…"
                        }
                        className="form-input w-full !py-2 !pl-9 !pr-12 text-[13px]"
                        data-testid="object-search"
                      />
                      {search ? (
                        <button
                          type="button"
                          onClick={() => setSearch("")}
                          aria-label="Clear search"
                          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                        >
                          <X className="h-4 w-4" />
                        </button>
                      ) : (
                        <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-semibold text-slate-400 dark:border-slate-700 dark:bg-slate-900">
                          ⌘K
                        </kbd>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <FilterSelect
                        label="Type"
                        allLabel="All types"
                        value={typeFilter}
                        onChange={setTypeFilter}
                        options={[
                          { value: "folder", label: "Folders" },
                          { value: "image", label: "Images" },
                          { value: "video", label: "Video" },
                          { value: "audio", label: "Audio" },
                          { value: "document", label: "Documents" },
                          { value: "code", label: "Code" },
                          { value: "text", label: "Text" },
                          { value: "archive", label: "Archives" },
                          { value: "other", label: "Other" },
                        ]}
                        className="w-40"
                      />
                      <label className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-slate-200/80 bg-white px-3 py-1.5 text-[12px] font-semibold text-slate-600 shadow-sm transition hover:border-caliber-200 dark:border-slate-700/70 dark:bg-slate-950/80 dark:text-slate-200">
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 rounded border-slate-300 text-caliber-purple accent-caliber-purple"
                          checked={searchAll}
                          onChange={(e) => {
                            setSearchAll(e.target.checked);
                            setSelectedKey(null);
                            setPreviewKey(null);
                            clearSelection();
                          }}
                        />
                        Search all folders
                      </label>
                      <ClearFiltersButton
                        visible={hasObjectBrowserFilters}
                        onClear={() => {
                          setSearch("");
                          setTypeFilter("");
                          setSearchAll(false);
                          setSelectedKey(null);
                          setPreviewKey(null);
                          clearSelection();
                        }}
                      />
                      <span className="rounded-full border border-slate-200/70 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80 dark:text-slate-200">
                        {folderCount} folder{folderCount === 1 ? "" : "s"} ·{" "}
                        {fileCount} file{fileCount === 1 ? "" : "s"}
                      </span>
                    </div>
                  </div>
                </div>

                {/* New folder inline */}
                {newFolderName !== null && (
                  <div className="border-b border-slate-200/60 bg-caliber-50/50 px-4 py-3 dark:border-slate-700/70 dark:bg-caliber-purple/10">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="grid h-9 w-9 place-items-center rounded-xl bg-white text-caliber-purple shadow-sm dark:bg-slate-950 dark:text-violet-200">
                        <FolderPlus className="h-4 w-4" />
                      </span>
                      <input
                        autoFocus
                        value={newFolderName}
                        onChange={(e) => setNewFolderName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") createFolder();
                          if (e.key === "Escape") setNewFolderName(null);
                        }}
                        placeholder="folder name"
                        className="form-input w-56 !py-2 text-[13px]"
                        data-testid="new-folder-input"
                      />
                      <button
                        type="button"
                        onClick={createFolder}
                        className="rounded-xl bg-caliber-purple px-3 py-2 text-xs font-bold text-white shadow-sm transition hover:bg-caliber-purple-dark"
                      >
                        Create
                      </button>
                      <button
                        type="button"
                        onClick={() => setNewFolderName(null)}
                        className="text-xs font-semibold text-slate-500 hover:text-slate-800"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {/* Bulk action bar */}
                {selected.size > 0 && (
                  <div
                    className="flex items-center gap-4 border-b border-caliber-200/60 bg-gradient-to-r from-caliber-50 to-violet-50/40 px-4 py-2 text-sm"
                    data-testid="bulk-bar"
                  >
                    <span className="inline-flex items-center gap-1.5 font-bold text-caliber-purple">
                      <CheckSquare className="h-4 w-4" />
                      {selected.size} selected
                    </span>
                    <button
                      type="button"
                      onClick={downloadSelected}
                      className="inline-flex items-center gap-1.5 font-semibold text-slate-700 transition hover:text-caliber-purple"
                    >
                      <Download className="h-4 w-4" /> Download
                    </button>
                    <button
                      type="button"
                      onClick={copySelectedKeys}
                      title="Copy the keys of all selected objects"
                      className="inline-flex items-center gap-1.5 font-semibold text-slate-700 transition hover:text-caliber-purple"
                    >
                      <Copy className="h-4 w-4" /> Copy keys
                    </button>
                    <button
                      type="button"
                      onClick={deleteSelected}
                      disabled={busy === "bulk-delete"}
                      className="inline-flex items-center gap-1.5 font-semibold text-red-600 transition hover:text-red-700 disabled:opacity-50"
                      data-testid="bulk-delete"
                    >
                      <Trash2 className="h-4 w-4" /> Delete
                    </button>
                    <button
                      type="button"
                      onClick={clearSelection}
                      className="ml-auto font-semibold text-slate-500 transition hover:text-slate-800"
                    >
                      Clear
                    </button>
                  </div>
                )}

                {/* Upload drop-zone (active during an upload) */}
                {uploadState && (
                  <div className="border-b border-slate-200/60 px-4 py-3">
                    <div className="rounded-xl border-2 border-dashed border-caliber-200 bg-caliber-50/40 px-4 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex min-w-0 items-center gap-2.5 text-sm">
                          <span className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-lg bg-white text-caliber-purple shadow-card">
                            <UploadCloud className="h-4 w-4" />
                          </span>
                          <div className="min-w-0">
                            <span className="font-semibold text-slate-700">
                              {uploadState.name}
                            </span>
                            <span className="text-slate-400">
                              {" "}
                              — uploading to{" "}
                              <span className="font-mono text-[12px]">
                                {searchAll ? "/" : prefix || "/"}
                              </span>
                            </span>
                          </div>
                        </div>
                        <div className="flex items-center gap-3">
                          <span className="text-xs font-semibold text-slate-500">
                            {uploadState.done}/{uploadState.total}
                          </span>
                          <span className="text-sm font-bold text-caliber-purple">
                            {uploadPct}%
                          </span>
                          <span
                            className="grid h-6 w-6 place-items-center rounded-md text-slate-300"
                            title="Cancel upload"
                          >
                            <X className="h-3.5 w-3.5" />
                          </span>
                        </div>
                      </div>
                      <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-white/80">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-caliber-purple to-blue-500 transition-[width]"
                          style={{ width: `${uploadPct}%` }}
                        />
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-slate-400">
                        <span className="inline-flex items-center gap-1">
                          <ShieldCheck className="h-3 w-3 text-emerald-500" />
                          Boundary checks: extension + content sniff
                        </span>
                        <span>Max 250 MB / file · denied: .exe .dll .bat .sh</span>
                        <span>
                          Drag &amp; drop anywhere on this list to upload here
                        </span>
                      </div>
                    </div>
                  </div>
                )}

                {/* Table */}
                <div className="min-w-0 flex-1 overflow-auto">
                  <table className="w-full min-w-[760px] table-fixed text-sm">
                    <colgroup>
                      <col className="w-11" />
                      <col />
                      <col className="w-24" />
                      <col className="w-44" />
                      <col className="w-44" />
                      <col className="w-40" />
                    </colgroup>
                    <thead>
                      <tr className="object-store-table-head border-b border-slate-200/60 bg-slate-50/70 text-[11px] uppercase tracking-[0.14em] text-slate-500">
                        <th className="px-4 py-2.5">
                          <input
                            type="checkbox"
                            className={checkboxClass}
                            checked={allSelected}
                            ref={(el) => {
                              if (el) el.indeterminate = someSelected;
                            }}
                            onChange={toggleSelectAll}
                            aria-label="Select all"
                            data-testid="select-all"
                          />
                        </th>
                        <SortableTh
                          label="Name"
                          sortKey="name"
                          current={sortKey}
                          dir={sortDir}
                          onSort={setSort}
                        />
                        <SortableTh
                          label="Size"
                          sortKey="size"
                          current={sortKey}
                          dir={sortDir}
                          onSort={setSort}
                          align="right"
                        />
                        <SortableTh
                          label="Created"
                          sortKey="created"
                          current={sortKey}
                          dir={sortDir}
                          onSort={setSort}
                        />
                        <SortableTh
                          label="Modified"
                          sortKey="modified"
                          current={sortKey}
                          dir={sortDir}
                          onSort={setSort}
                        />
                        <th className="px-4 py-2.5 text-right font-bold">
                          Actions
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {objectsQuery.isLoading && (
                        <tr>
                          <td
                            colSpan={6}
                            className="px-5 py-12 text-center text-slate-500"
                          >
                            Loading…
                          </td>
                        </tr>
                      )}

                      {!searchAll && prefix && !objectsQuery.isLoading && (
                        <tr className="object-store-row transition hover:bg-slate-50/70">
                          <td />
                          <td colSpan={5} className="px-2 py-2">
                            <button
                              type="button"
                              onClick={() =>
                                navigate(
                                  segments.slice(0, -1).length
                                    ? `${segments.slice(0, -1).join("/")}/`
                                    : "",
                                )
                              }
                              className="inline-flex items-center gap-2 rounded-lg px-2 py-1 font-semibold text-slate-500 transition hover:bg-caliber-50 hover:text-caliber-purple"
                            >
                              <CornerLeftUp className="h-4 w-4" /> ..
                            </button>
                          </td>
                        </tr>
                      )}

                      {folders.map((f) => (
                        <tr
                          key={f.prefix}
                          className="object-store-row group transition hover:bg-slate-50/70"
                        >
                          <td />
                          <td className="min-w-0 px-2 py-2.5">
                            <button
                              type="button"
                              onClick={() => navigate(f.prefix)}
                              title="Open folder"
                              className="object-store-file-name flex w-full min-w-0 items-center gap-2 rounded-lg px-2 py-1 text-left font-bold text-slate-800 transition hover:bg-amber-50 hover:text-amber-700"
                            >
                              <Folder className="h-4 w-4 flex-shrink-0 text-amber-500" />
                              <span className="block min-w-0 truncate">
                                {f.label}/
                              </span>
                            </button>
                          </td>
                          <td className="px-4 py-2.5 text-right text-slate-300">
                            —
                          </td>
                          <td className="px-4 py-2.5 text-slate-300">—</td>
                          <td className="px-4 py-2.5 text-slate-300">—</td>
                          <td className="px-4 py-2.5 text-right">
                            <button
                              type="button"
                              onClick={() => deleteFolder(f.prefix)}
                              title="Delete folder and contents"
                              className={`${iconButtonClass} opacity-0 hover:text-red-600 group-hover:opacity-100`}
                            >
                              <Trash2 className="h-4 w-4" />
                            </button>
                          </td>
                        </tr>
                      ))}

                      {files.map((o) => {
                        const Icon = fileIconFor(o.label);
                        const isSel = selected.has(o.key);
                        const isOpen = selectedKey === o.key;
                        return (
                          <tr
                            key={o.key}
                            className={`group transition ${
                              isOpen
                                ? "object-store-open-row bg-amber-50/80 shadow-[inset_3px_0_0_#f59e0b]"
                                : isSel
                                  ? "object-store-selected-row bg-caliber-50/60"
                                  : "object-store-row hover:bg-slate-50/70"
                            }`}
                            data-testid={`object-${o.key}`}
                          >
                            <td className="px-4 py-2.5">
                              <input
                                type="checkbox"
                                className={checkboxClass}
                                checked={isSel}
                                onChange={() => toggleSelect(o.key)}
                                aria-label={`Select ${o.label}`}
                              />
                            </td>
                            <td className="min-w-0 px-2 py-2.5">
                              <button
                                type="button"
                                onClick={() => openObject(o.key)}
                                title={searchAll ? "Open object" : o.label}
                                className={`object-store-file-name flex w-full min-w-0 items-center gap-2 rounded-lg px-2 py-1 text-left transition ${
                                  isOpen
                                    ? "font-bold text-amber-700"
                                    : "font-semibold text-slate-800 hover:bg-caliber-50 hover:text-caliber-purple"
                                }`}
                              >
                                <Icon
                                  className={`h-4 w-4 flex-shrink-0 ${isOpen ? "text-amber-600" : "text-slate-400"}`}
                                />
                                <span className="block min-w-0 truncate">
                                  {o.label}
                                </span>
                              </button>
                            </td>
                            <td className="px-4 py-2.5 text-right font-medium text-slate-500">
                              {humanSize(o.size)}
                            </td>
                            <td className="px-4 py-2.5 text-slate-500">
                              {fmtDate(o.created_at ?? o.last_modified)}
                            </td>
                            <td className="px-4 py-2.5 text-slate-500">
                              {fmtDate(o.last_modified)}
                            </td>
                            <td className="whitespace-nowrap px-4 py-2.5 text-right">
                              <button
                                type="button"
                                onClick={() => openPreview(o.key)}
                                title="View preview"
                                className={iconButtonClass}
                              >
                                <Eye className="h-4 w-4" />
                              </button>
                              <button
                                type="button"
                                onClick={() => openObject(o.key)}
                                title="Open in new tab"
                                className={`ml-0.5 ${iconButtonClass}`}
                              >
                                <ExternalLink className="h-4 w-4" />
                              </button>
                              <a
                                href={caliberApi.objectStoreDownloadUrl(
                                  selectedBucket,
                                  o.key,
                                )}
                                title="Download"
                                className={`ml-0.5 ${iconButtonClass}`}
                              >
                                <Download className="h-4 w-4" />
                              </a>
                              <button
                                type="button"
                                onClick={() => copyKey(o.key)}
                                title="Copy key"
                                className={`ml-0.5 ${iconButtonClass}`}
                              >
                                {copiedKey === o.key ? (
                                  <Check className="h-4 w-4 text-emerald-500" />
                                ) : (
                                  <Copy className="h-4 w-4" />
                                )}
                              </button>
                              <button
                                type="button"
                                onClick={() => deleteOne(o.key)}
                                title="Delete"
                                className={`ml-0.5 ${iconButtonClass} hover:bg-red-50 hover:text-red-600`}
                              >
                                <Trash2 className="h-4 w-4" />
                              </button>
                            </td>
                          </tr>
                        );
                      })}

                      {!objectsQuery.isLoading &&
                        !folders.length &&
                        !files.length && (
                          <tr>
                            <td
                              colSpan={6}
                              className="px-5 py-14 text-center text-sm text-slate-500"
                            >
                              {q
                                ? `No matches for "${search}".`
                                : searchAll
                                  ? "No objects in this bucket."
                                  : "This folder is empty. Drag files here or use Upload."}
                            </td>
                          </tr>
                        )}
                    </tbody>
                  </table>
                </div>

                {listing?.is_truncated && (
                  <div className="border-t border-amber-200/70 bg-amber-50/80 px-4 py-2 text-xs font-medium text-amber-700">
                    Showing the first {files.length} results (truncated) —
                    narrow your search.
                  </div>
                )}

                {/* Pagination / summary footer (static — single page) */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200/60 bg-slate-50/50 px-4 py-2 text-[11px] text-slate-400 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-300">
                  <span>
                    Showing{" "}
                    <span className="font-semibold text-slate-500 dark:text-slate-100">
                      1–{fileCount}
                    </span>{" "}
                    of{" "}
                    <span className="font-semibold text-slate-500 dark:text-slate-100">
                      {fileCount}
                    </span>{" "}
                    objects · {folderCount} folders · {inViewSize} in view
                  </span>
                </div>
                <div className="flex items-center justify-center border-t border-slate-200/60 bg-slate-50/50 px-4 py-1.5 text-[11px] text-slate-400 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-300">
                  <span className="inline-flex items-center gap-1.5">
                    <ShieldCheck className="h-3.5 w-3.5 text-emerald-500" />
                    Uploads validated at the boundary — extension allow-list +
                    content sniff + traversal &amp; zip-bomb defenses
                  </span>
                </div>

                {dragOver && (
                  <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center border-2 border-dashed border-caliber-purple bg-caliber-50/90 backdrop-blur-sm">
                    <div className="flex items-center gap-2 rounded-2xl bg-white px-5 py-3 font-bold text-caliber-purple shadow-xl">
                      <Upload className="h-5 w-5" /> Drop files to upload to{" "}
                      {prefix || "/"}
                    </div>
                  </div>
                )}
              </div>

            </div>
          )}
        </div>
      </div>

      {previewKey && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/65 px-4 py-8 backdrop-blur-sm"
          onClick={(event) => {
            if (event.target === event.currentTarget) closePreview();
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="object-preview-title"
            data-testid="object-preview-modal"
            className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-[28px] border border-slate-200/70 bg-white shadow-2xl dark:border-slate-700/70 dark:bg-slate-950"
          >
            <div className="border-b border-slate-200/60 bg-gradient-to-r from-slate-50 via-white to-slate-50/80 px-6 py-5 dark:border-slate-700/70 dark:bg-slate-950/95">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className={labelClass}>Object preview</div>
                  <h2
                    id="object-preview-title"
                    className="mt-1 truncate text-lg font-semibold text-slate-900"
                  >
                    {previewKey}
                  </h2>
                  <p className="mt-1 text-sm text-slate-500">
                    Previewing content directly from{" "}
                    <span className="font-mono text-slate-600">
                      {selectedBucket}
                    </span>
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {previewDownloadUrl && (
                    <a
                      href={previewDownloadUrl}
                      className="btn-ghost"
                    >
                      <Download className="h-4 w-4" />
                      Download
                    </a>
                  )}
                  {previewViewUrl && (
                    <a
                      href={previewViewUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="btn-ghost"
                    >
                      <ExternalLink className="h-4 w-4" />
                      Open in new tab
                    </a>
                  )}
                  <button
                    type="button"
                    onClick={closePreview}
                    aria-label="Close file preview"
                    className="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-500 transition hover:bg-slate-50 hover:text-slate-700 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>

            <div className="border-b border-slate-200/60 px-6 py-4 dark:border-slate-700/70">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/80 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/80">
                  <div className={labelClass}>Created</div>
                  <div className="mt-1 text-sm font-semibold text-slate-700">
                    {fmtDate(
                      previewData?.created_at ?? previewData?.last_modified ?? null,
                    )}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/80 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/80">
                  <div className={labelClass}>Modified</div>
                  <div className="mt-1 text-sm font-semibold text-slate-700">
                    {fmtDate(previewData?.last_modified ?? null)}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/80 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/80">
                  <div className={labelClass}>Size</div>
                  <div className="mt-1 text-sm font-semibold text-slate-700">
                    {previewData ? humanSize(previewData.size) : "Loading…"}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200/70 bg-slate-50/80 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/80">
                  <div className={labelClass}>Content type</div>
                  <div className="mt-1 truncate text-sm font-semibold text-slate-700">
                    {previewData?.content_type ?? "Loading…"}
                  </div>
                </div>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto px-6 py-5">
              {previewQuery.isLoading && (
                <div className="flex min-h-[320px] items-center justify-center rounded-2xl border border-slate-200/70 bg-slate-50/80 text-sm text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80">
                  Loading preview…
                </div>
              )}

              {previewQuery.error && (
                <div className="rounded-2xl border border-red-200/70 bg-red-50 px-5 py-4 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200">
                  {previewQuery.error.message}
                </div>
              )}

              {!previewQuery.isLoading && !previewQuery.error && previewData && (
                <>
                  {previewKind === "markdown" && (
                    <MarkdownView source={previewData.text ?? ""} />
                  )}

                  {previewKind === "csv" && (
                    <DataTable
                      sheets={[
                        {
                          name: "Sheet 1",
                          rows: parseDelimited(
                            previewData.text ?? "",
                            extOf(previewKey) === "tsv" ? "\t" : ",",
                          ),
                        },
                      ]}
                      truncated={previewData.truncated}
                    />
                  )}

                  {previewData.is_text &&
                    previewKind !== "markdown" &&
                    previewKind !== "csv" && (
                      <pre className="max-h-[60vh] overflow-auto rounded-2xl border border-slate-200/70 bg-slate-950 px-5 py-4 text-xs leading-relaxed text-slate-100">
                        {previewData.text || "(empty file)"}
                      </pre>
                    )}

                  {previewKind === "image" && previewViewUrl && (
                    <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4 text-center dark:border-slate-700/70 dark:bg-slate-900/80">
                      <img
                        src={previewViewUrl}
                        alt={previewKey}
                        className="mx-auto max-h-[60vh] rounded-xl object-contain shadow-card"
                      />
                    </div>
                  )}

                  {previewKind === "pdf" && previewViewUrl && (
                    <iframe
                      title={previewKey}
                      src={previewViewUrl}
                      className="h-[70vh] w-full rounded-2xl border border-slate-200/70 bg-white dark:border-slate-700/70 dark:bg-slate-950"
                    />
                  )}

                  {previewKind === "audio" && previewViewUrl && (
                    <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-6 dark:border-slate-700/70 dark:bg-slate-900/80">
                      <audio
                        controls
                        src={previewViewUrl}
                        className="w-full"
                        data-testid="object-preview-audio"
                      />
                    </div>
                  )}

                  {previewKind === "video" && previewViewUrl && (
                    <div className="rounded-2xl border border-slate-200/70 bg-black p-2 text-center dark:border-slate-700/70">
                      <video
                        controls
                        src={previewViewUrl}
                        className="mx-auto max-h-[64vh] w-full rounded-xl"
                        data-testid="object-preview-video"
                      />
                    </div>
                  )}

                  {previewKind === "office" && (
                    <>
                      {extractQuery.isLoading && (
                        <div className="flex min-h-[200px] items-center justify-center rounded-2xl border border-slate-200/70 bg-slate-50/80 text-sm text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/80">
                          Extracting content…
                        </div>
                      )}
                      {extractQuery.error && (
                        <div className="rounded-2xl border border-red-200/70 bg-red-50 px-5 py-4 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200">
                          {extractQuery.error.message}
                        </div>
                      )}
                      {extractQuery.data?.kind === "document" && (
                        <div
                          data-testid="object-preview-document"
                          className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-2xl border border-slate-200/70 bg-white px-6 py-4 text-sm leading-relaxed text-slate-700 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-200"
                        >
                          {extractQuery.data.text || "(no extractable text)"}
                        </div>
                      )}
                      {extractQuery.data?.kind === "sheet" && (
                        <DataTable
                          sheets={extractQuery.data.sheets ?? []}
                          truncated={extractQuery.data.truncated}
                        />
                      )}
                      {extractQuery.data?.kind === "unsupported" && (
                        <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center dark:border-slate-700/70">
                          <div className="text-sm font-semibold text-slate-600">
                            Inline preview is not available for this file.
                          </div>
                          <div className="mt-1.5 text-xs text-slate-400">
                            {extractQuery.data.error ||
                              "Use the actions above to open or download it."}
                          </div>
                        </div>
                      )}
                    </>
                  )}

                  {previewKind === "none" && !previewData.is_text && (
                    <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-hero px-8 py-12 text-center dark:border-slate-700/70">
                      <div className="text-sm font-semibold text-slate-600">
                        Inline preview is not available for this file type.
                      </div>
                      <div className="mt-1.5 text-xs text-slate-400">
                        Use the actions above to open the file in a new tab or
                        download it.
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>

            {previewData?.truncated && (
              <div className="border-t border-amber-200/70 bg-amber-50/80 px-6 py-3 text-xs font-medium text-amber-700">
                Preview truncated after {humanSize(previewData.preview_bytes)}.
                Open the full object in a new tab for the complete file.
              </div>
            )}
          </div>
        </div>
      )}

    </div>
  );
}
