import { execFileSync } from 'node:child_process';

export class GitHub {
  constructor(run = execFileSync) { this.run = run; }
  gh(args, input) {
    try {
      const value = this.run('gh', args, {
        encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
        input: input === undefined ? undefined : JSON.stringify(input),
        stdio: ['pipe', 'pipe', 'pipe'], timeout: 120000,
      });
      return value.trim() ? JSON.parse(value) : null;
    } catch (error) {
      // Never echo submitted issue bodies or credentials through subprocess error messages.
      throw new Error(`GitHub command failed (${args[0]}): ${Number.isInteger(error.status) ? `exit ${error.status}` : 'unavailable command or invalid response'}; inspect gh authentication, permissions and API availability without publishing sensitive diagnostics`);
    }
  }
  graphql(query, variables = {}) {
    const response = this.gh(['api', 'graphql', '--input', '-'], { query, variables });
    if (response.errors?.length) throw new Error(`GitHub GraphQL: ${response.errors.map(e => e.message).join('; ')}`);
    if (!response.data) throw new Error('GitHub returned no data');
    return response.data;
  }
  async snapshot(config) {
    // Project read is deliberately first; an unavailable project must not fall back to a local plan.
    const project = this.gh(['project', 'view', String(config.project.number), '--owner', config.project.owner, '--format', 'json']);
    if (!project?.id || project.closed) throw new Error('Configured GitHub Project is unavailable or closed');
    const fields = [];
    let cursor = null;
    do {
      const page = this.graphql(`query($id:ID!,$after:String){node(id:$id){... on ProjectV2{fields(first:100,after:$after){nodes{... on ProjectV2Field{id name dataType} ... on ProjectV2SingleSelectField{id name dataType options{id name}} ... on ProjectV2IterationField{id name dataType}} pageInfo{hasNextPage endCursor}}}}}`, { id: project.id, after: cursor }).node.fields;
      fields.push(...page.nodes);
      cursor = nextCursor(page);
    } while (cursor);
    const [owner, name] = config.repository.split('/');
    const repository = this.graphql(`query($owner:String!,$name:String!){repository(owner:$owner,name:$name){nameWithOwner url defaultBranchRef{name target{oid}}}}`, { owner, name }).repository;
    if (!repository?.defaultBranchRef) throw new Error('Repository/default branch is unavailable');
    const milestones = this.gh(['api', '--paginate', '--slurp', `repos/${config.repository}/milestones?state=all&per_page=100`]).flat().map(m => ({ id: m.node_id, number: m.number, title: m.title, state: m.state.toUpperCase(), description: m.description || '' }));
    const items = [];
    cursor = null;
    do {
      const page = this.graphql(PROJECT_ITEMS_QUERY, { id: project.id, after: cursor }).node.items;
      for (const item of page.nodes) {
        ensureComplete(item.fieldValues, `project item ${item.id} field values`);
        if (item.content?.__typename !== 'Issue') {
          items.push({ id: item.id, isArchived: item.isArchived, fields: values(item.fieldValues.nodes), issue: null, contentType: item.content?.__typename || 'Redacted' });
          continue;
        }
        const issue = item.content;
        for (const key of ['labels', 'assignees', 'blockedBy', 'closedByPullRequestsReferences', 'subIssues']) ensureComplete(issue[key], `issue #${issue.number} ${key}`);
        items.push({ id: item.id, isArchived: item.isArchived, fields: values(item.fieldValues.nodes), issue: {
          ...issue, repository: issue.repository.nameWithOwner,
          labels: issue.labels.nodes.map(x => x.name), assignees: issue.assignees.nodes.map(x => x.login),
          blockedBy: issue.blockedBy.nodes.map(x => ({ ...x, repository: x.repository.nameWithOwner })),
          prs: issue.closedByPullRequestsReferences.nodes, subIssues: issue.subIssues.nodes,
        } });
      }
      cursor = nextCursor(page);
    } while (cursor);
    return { capturedAt: new Date().toISOString(), repository: { nameWithOwner: repository.nameWithOwner, url: repository.url, defaultBranch: repository.defaultBranchRef.name, headOid: repository.defaultBranchRef.target.oid }, project, fields, milestones, items };
  }
  labels(repo) { return this.gh(['api', '--paginate', '--slurp', `repos/${repo}/labels?per_page=100`]).flat().map(label => label.name); }
  allIssues(repo) {
    return this.gh(['api', '--paginate', '--slurp', `repos/${repo}/issues?state=all&per_page=100`]).flat().filter(x => !x.pull_request);
  }
  issue(repo, number) { return this.gh(['api', `repos/${repo}/issues/${number}`]); }
  createIssue(repo, payload) { return this.gh(['api', `repos/${repo}/issues`, '--method', 'POST', '--input', '-'], payload); }
  updateIssue(repo, number, payload) { return this.gh(['api', `repos/${repo}/issues/${number}`, '--method', 'PATCH', '--input', '-'], payload); }
  addItem(projectId, contentId) {
    return this.graphql(`mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}`, { p: projectId, c: contentId }).addProjectV2ItemById.item.id;
  }
  setField(projectId, itemId, field, value) {
    const encoded = field.dataType === 'SINGLE_SELECT'
      ? { singleSelectOptionId: field.options.find(x => x.name === value)?.id }
      : { text: value };
    if (field.dataType === 'SINGLE_SELECT' && !encoded.singleSelectOptionId) throw new Error(`Unknown ${field.name} option: ${value}`);
    if (!['TEXT', 'SINGLE_SELECT'].includes(field.dataType)) throw new Error(`Unsupported writable field type: ${field.dataType}`);
    return this.graphql(`mutation($p:ID!,$i:ID!,$f:ID!,$v:ProjectV2FieldValue!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:$v}){projectV2Item{id}}}`, { p: projectId, i: itemId, f: field.id, v: encoded });
  }
  addDependency(issueId, blockerId) {
    return this.graphql(`mutation($i:ID!,$b:ID!){addBlockedBy(input:{issueId:$i,blockingIssueId:$b}){issue{id}}}`, { i: issueId, b: blockerId });
  }
  comment(repo, number, body) { return this.gh(['api', `repos/${repo}/issues/${number}/comments`, '--method', 'POST', '--input', '-'], { body }); }
}

export function ensureComplete(connection, label) {
  if (!connection || !Array.isArray(connection.nodes) || connection.pageInfo?.hasNextPage !== false) throw new Error(`Incomplete ${label}; inspect/paginate this connection before continuing`);
}
function nextCursor(page) {
  if (!Array.isArray(page?.nodes) || typeof page.pageInfo?.hasNextPage !== 'boolean') throw new Error('Malformed pagination metadata');
  if (!page.pageInfo.hasNextPage) return null;
  if (!page.pageInfo.endCursor) throw new Error('Pagination cursor missing');
  return page.pageInfo.endCursor;
}
export function values(nodes) {
  const result = {};
  for (const node of nodes) {
    if (!node.field?.name) continue;
    result[node.field.name] = node.text ?? node.name ?? node.number ?? node.date ?? node.title ?? null;
  }
  return result;
}

const PROJECT_ITEMS_QUERY = `query($id:ID!,$after:String){node(id:$id){... on ProjectV2{items(first:50,after:$after){nodes{id isArchived fieldValues(first:100){nodes{... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}} ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}} ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}} ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{name}}} ... on ProjectV2ItemFieldIterationValue{title field{... on ProjectV2FieldCommon{name}}}} pageInfo{hasNextPage}} content{__typename ... on Issue{id number title body state stateReason url updatedAt repository{nameWithOwner} milestone{id number title state description} parent{number url} labels(first:100){nodes{name} pageInfo{hasNextPage}} assignees(first:100){nodes{login} pageInfo{hasNextPage}} blockedBy(first:100){nodes{id number state stateReason url repository{nameWithOwner}} pageInfo{hasNextPage}} subIssues(first:100){nodes{number state url} pageInfo{hasNextPage}} closedByPullRequestsReferences(first:100){nodes{number url state mergedAt headRefOid} pageInfo{hasNextPage}}}}} pageInfo{hasNextPage endCursor}}}}}`;
