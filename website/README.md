# Veetbot public website

Source for the public Veetbot homepage and Google OAuth policy pages:

- `https://www.veetbot.com/`
- `https://www.veetbot.com/privacy`
- `https://www.veetbot.com/tos`

The site is a static public surface hosted by Nginx on the Veetbot production
Droplet. It has no accounts, forms, analytics, database, or access to Veetbot
credentials.

## Local development

Requires Node.js 22.13 or newer.

```bash
npm install
npm run dev
```

Run `npm test` for a production build plus rendered route assertions, and
`npm run lint` for source linting.

`npm run build` writes the deployable static artifact to `out/`. The main
repository's atomic Nginx deployment packages that directory with the same
release identity as the application and documentation artifacts.
