export function artworkDocument(text) {
  const content=String(text||'').trim().replace(/^```(?:html|svg|xml)?\s*\n?/i,'').replace(/\n?```\s*$/,'');
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; form-action 'none'; base-uri 'none'"><style>body{margin:0;background:#fff}svg{max-width:100%;height:auto}</style>${content}`;
}
