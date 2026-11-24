CREATE TABLE public.payments (
    id serial PRIMARY KEY,
    amount numeric(12,2)
);

ALTER TABLE public.payments ADD COLUMN invoice_ref integer;
ALTER TABLE public.payments ADD CONSTRAINT payments_invoice_fk FOREIGN KEY (invoice_ref) REFERENCES public.invoices (id);
