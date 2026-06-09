# Launch checklist — pointing nailcollab.org at this site

Steps to switch the domain from Google Sites to this GitHub Pages site, plus a few
post-launch items. Most take a minute or two each.

## 1. Custom domain (do these together)

- [ ] In the domain registrar's DNS, point the domain at GitHub Pages:
  - `www` CNAME record → `mmichalowski.github.io`
  - Apex/root (`nailcollab.org`) A records → `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
- [ ] In this repo: Settings → Pages → Custom domain → enter `www.nailcollab.org`
  (this creates the `CNAME` file). **Don't add the CNAME file before the DNS
  switch** — it would break the github.io preview while the domain still points
  to Google Sites.
- [ ] Tick "Enforce HTTPS" once the certificate is provisioned (can take ~1 hour).
- [ ] Keep the `ainurse26.nailcollab.org` DNS record as-is — the AINurse-26
  workshop site lives on that subdomain.

## 2. Old Google Sites pages

- [ ] The old `/brocher-2024` page will no longer be reachable at
  `nailcollab.org/brocher-2024` after the switch. The site now links to its
  permanent address instead: <https://sites.google.com/view/nailcollab/brocher-2024>.
  Keep that Google Site published so the link keeps working.
- [ ] A styled `404.html` is included — anyone hitting an old URL gets a branded
  page that redirects home.

## 3. Analytics (optional, recommended)

- [ ] Create a free account at <https://www.goatcounter.com> (privacy-friendly,
  no cookies, no GDPR banner needed). Pick a code, e.g. `nailcollab`.
- [ ] In `index.html`, search for `goatcounter`, replace `MYCODE` with your code,
  and remove the surrounding HTML comment markers.

## 4. After the domain is live

- [ ] Share the site once on LinkedIn and check the link preview shows the navy
  card image (`social-card.png`). If LinkedIn shows a stale preview, run the URL
  through <https://www.linkedin.com/post-inspector/>.
- [ ] Optionally add the site to Google Search Console (verify via DNS TXT
  record) and submit `sitemap.xml`.

## Editing content later

`index.html` contains copy-paste templates as HTML comments right where content
lives — search for "TO ADD A NEWS ITEM", "TO ADD A PUBLICATION", or
"TO ADD AN EVENT". Edits can be made directly in the GitHub web editor.

The "wider network" list on the Leadership page was derived from co-author lists
of the collaborative's joint publications — please review and adjust names as
the group sees fit (search for "net-chip").
