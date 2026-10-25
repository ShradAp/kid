# Third-party notices

The original application code is distributed under GPL-3.0-only; see [LICENSE](LICENSE).
Third-party components retain their own copyright notices and licenses.

## shadcn/ui

The generated UI primitives in `UI/src/components/ui/` and the vendored
`UI/src/styles/shadcn.css` are derived from shadcn/ui and shadcn 4.11.0.
Copyright (c) 2023 shadcn. These components are available under the MIT License;
the complete notice is preserved in [shadcn-LICENSE.md](UI/src/styles/shadcn-LICENSE.md).

Source: https://github.com/shadcn-ui/ui

## Installed dependencies

Python and JavaScript dependencies are installed separately using
`requirements.txt` and `UI/package-lock.json`. Their source and license files are
not replaced by the application's GPL license. Keep their required notices when
redistributing bundled dependencies, executables, or browser runtimes.

The optional SearXNG container is a separate AGPL-licensed service; it is downloaded
from its upstream image and is not included in this source repository.
