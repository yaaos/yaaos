import {
  type Lesson,
  useCreateLesson,
  useDeleteLesson,
  useGithubRepositories,
  useLessons,
} from "@core/api";
import { ErrorBanner, PageHeader } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Textarea } from "@shared/components/ui/textarea";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

type LessonSort = "created_desc" | "created_asc" | "updated_desc";

export function LessonsPage() {
  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-4 p-6">
      <PageHeader
        title="Lessons"
        subtitle="Repository-scoped notes the reviewer agent consults during reviews."
      />
      <LessonsForm />
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load lessons." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="rounded-lg border border-border bg-card">
              <header className="border-b border-border px-4 py-3">
                <h2 className="text-sm font-semibold">Lessons</h2>
              </header>
              <div className="px-4 py-4 flex flex-col gap-2">
                {Array.from({ length: 3 }).map((_, i) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: skeleton rows
                  <Skeleton key={i} className="h-16" />
                ))}
              </div>
            </div>
          }
        >
          <LessonsList />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function LessonsForm() {
  const { data: repos } = useGithubRepositories();
  const create = useCreateLesson();

  const [repoExternalId, setRepoExternalId] = useState<string>("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  const repoNamesFromInstall = new Set(
    (repos?.repositories ?? []).map((r) => r.full_name).filter(Boolean) as string[],
  );
  const pickerOptions = Array.from(repoNamesFromInstall).sort();

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Add a lesson</h2>
      </header>
      <div className="px-4 py-4">
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!repoExternalId || !title.trim() || !body.trim()) return;
            create.mutate(
              { repo_external_id: repoExternalId, title: title.trim(), body: body.trim() },
              {
                onSuccess: () => {
                  setTitle("");
                  setBody("");
                },
              },
            );
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="lesson-repo">Repo</Label>
            <Select value={repoExternalId} onValueChange={setRepoExternalId}>
              <SelectTrigger id="lesson-repo" data-testid="lesson-repo">
                <SelectValue placeholder="(select a repo)" />
              </SelectTrigger>
              <SelectContent>
                {pickerOptions.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {pickerOptions.length === 0 && (
              <p className="text-muted-foreground text-xs">
                No repos yet. Install the GitHub App on at least one repo to populate this list.
              </p>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="lesson-title">Title</Label>
            <Input
              id="lesson-title"
              data-testid="lesson-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="One-line summary"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="lesson-body">Body</Label>
            <Textarea
              id="lesson-body"
              data-testid="lesson-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="≤1000 chars"
              maxLength={1000}
              rows={4}
            />
          </div>
          <div>
            <Button type="submit" data-testid="lesson-save" disabled={create.isPending}>
              {create.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
          {create.isError && (
            <p className="text-destructive text-xs" data-testid="lesson-error">
              {(create.error as Error).message}
            </p>
          )}
        </form>
      </div>
    </section>
  );
}

function LessonsList() {
  const [q, setQ] = useState("");
  const [repoFilter, setRepoFilter] = useState<string>("all");
  const [sort, setSort] = useState<LessonSort>("created_desc");
  const { data: repos } = useGithubRepositories();
  const { data: lessons } = useLessons({
    q: q.trim() || undefined,
    repos: repoFilter === "all" ? undefined : [repoFilter],
    sort,
  });
  const remove = useDeleteLesson();

  const repoNamesFromLessons = new Set(lessons.map((l) => l.repo_external_id));
  const repoNamesFromInstall = new Set(
    (repos?.repositories ?? []).map((r) => r.full_name).filter(Boolean) as string[],
  );
  const pickerOptions = Array.from(
    new Set([...repoNamesFromInstall, ...repoNamesFromLessons]),
  ).sort();

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3 flex flex-wrap items-end gap-3">
        <h2 className="text-sm font-semibold mr-auto">Lessons</h2>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="lessons-search" className="text-xs">
            Search
          </Label>
          <Input
            id="lessons-search"
            data-testid="lessons-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="title or body…"
            className="h-8 w-[200px]"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="lessons-repo-filter" className="text-xs">
            Repo
          </Label>
          <Select value={repoFilter} onValueChange={setRepoFilter}>
            <SelectTrigger
              id="lessons-repo-filter"
              data-testid="lessons-repo-filter"
              className="h-8 w-[180px]"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All repos</SelectItem>
              {pickerOptions.map((name) => (
                <SelectItem key={name} value={name}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="lessons-sort" className="text-xs">
            Sort
          </Label>
          <Select value={sort} onValueChange={(v) => setSort(v as LessonSort)}>
            <SelectTrigger id="lessons-sort" data-testid="lessons-sort" className="h-8 w-[150px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="created_desc">Newest first</SelectItem>
              <SelectItem value="created_asc">Oldest first</SelectItem>
              <SelectItem value="updated_desc">Recently updated</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </header>
      <div className="px-4 py-4">
        <ul className="flex flex-col gap-2" data-testid="lessons-list">
          {lessons.map((l: Lesson) => (
            <li key={l.id} className="rounded-md border border-border bg-background p-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="font-medium flex-1">{l.title}</span>
                <span className="font-mono text-muted-foreground text-xs">
                  {l.repo_external_id}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => remove.mutate(l.id)}
                  className="text-destructive hover:text-destructive"
                >
                  Delete
                </Button>
              </div>
              <p className="text-muted-foreground mt-1 whitespace-pre-wrap text-xs">{l.body}</p>
            </li>
          ))}
          {lessons.length === 0 && (
            <li className="text-muted-foreground text-sm">No lessons yet.</li>
          )}
        </ul>
      </div>
    </section>
  );
}
