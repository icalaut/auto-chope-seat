import { mkdir, copyFile } from "node:fs/promises";
await mkdir(new URL("../dist/", import.meta.url), { recursive: true });
for (const file of ["index.html", "app.js", "style.css", "stations.json", "timezones.json"]) {
  await copyFile(new URL(`../${file}`, import.meta.url), new URL(`../dist/${file}`, import.meta.url));
}
