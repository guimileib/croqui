-- Mounted by docker-compose into /docker-entrypoint-initdb.d, pg_dump style.
CREATE TABLE "public"."accounts" (
    id integer NOT NULL,
    name text
);

CREATE TABLE public.invoices (
    id integer NOT NULL,
    account_id integer NOT NULL
);

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_account_fk FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;
