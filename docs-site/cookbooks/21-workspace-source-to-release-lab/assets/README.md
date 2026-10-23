# SCN-21 assets

**There are no asset files, by design.** Every artifact in this lab -- the prompt and its two
versions, the test set, the revisions, the change request, the release -- is created live through
the product, because the lab's subject *is* the machinery that pins and reviews those artifacts.
Shipping a pre-baked dataset would bury the point.

The three constants the release request needs (`environment_config_sha256`,
`runtime_dependencies_sha256`, `policy_sha256`) are content addresses: any 64 lowercase hex
characters. The script uses recognizable placeholders (`a…`, `b…`, `c…`); a real pipeline would
hash its environment config, its lockfile and its policy document and pass those.
