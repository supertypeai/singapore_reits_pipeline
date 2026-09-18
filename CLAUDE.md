Always talk in ASD-STE100 Simplified Technical English. Always read CONTEXT.md files (if there), and use their ubiquitous language.

Don't Co-Author yourself in commit message, also make commit message into using commitlint. No need make it too complex, simple, standart, easy to be understood.

Dont overcomment on small things (e.g. we create this because we face some issue in test XYZ). 

Add comment simply and when it is very-very necessary using standart practices or simple english sentences.

## Prod materialized view

`sgx_reit_report` is a materialized view. It holds a stored copy of the data, so it does not
follow the tables. Always refresh it after you change prod data or the schema:

```sql
refresh materialized view public.sgx_reit_report;
```

This applies to every `promote_final_to_prod.py --write` run, every migration in
`schema/migrations/`, and every direct fix on a prod table. PostgREST cannot refresh a view,
so run it in the Supabase SQL editor. Without it, prod serves the old values.