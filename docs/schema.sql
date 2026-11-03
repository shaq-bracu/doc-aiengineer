-- Generated from app/models.py (PostgreSQL dialect). SQLAlchemy creates this automatically on startup.

CREATE TABLE documents (
	id SERIAL NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	stored_path VARCHAR(500) NOT NULL, 
	file_type VARCHAR(10) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	stage VARCHAR(30) NOT NULL, 
	progress FLOAT NOT NULL, 
	page_count INTEGER NOT NULL, 
	ocr_pages INTEGER NOT NULL, 
	mean_ocr_conf FLOAT, 
	attempts INTEGER NOT NULL, 
	error TEXT, 
	extracted_text TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	duration_ms INTEGER, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_documents_sha256 ON documents (sha256);
CREATE INDEX ix_documents_status ON documents (status);

CREATE TABLE chunks (
	id SERIAL NOT NULL, 
	document_id INTEGER NOT NULL, 
	invoice_id INTEGER, 
	page INTEGER, 
	text TEXT NOT NULL, 
	emb_model VARCHAR(80) NOT NULL, 
	embedding BYTEA NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX ix_chunks_document_id ON chunks (document_id);
CREATE INDEX ix_chunks_emb_model ON chunks (emb_model);

CREATE TABLE invoices (
	id SERIAL NOT NULL, 
	document_id INTEGER NOT NULL, 
	segment_index INTEGER NOT NULL, 
	page_start INTEGER, 
	page_end INTEGER, 
	source_ref VARCHAR(120), 
	invoice_number VARCHAR(80), 
	invoice_date VARCHAR(10), 
	supplier_name VARCHAR(255), 
	customer_name VARCHAR(255), 
	subtotal FLOAT, 
	tax FLOAT, 
	total FLOAT, 
	currency VARCHAR(3), 
	line_items JSON NOT NULL, 
	confidence JSON NOT NULL, 
	extraction_notes JSON NOT NULL, 
	method VARCHAR(30) NOT NULL, 
	review_status VARCHAR(20) NOT NULL, 
	edited BOOLEAN NOT NULL, 
	dedupe_key VARCHAR(80), 
	raw_text TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX ix_invoices_dedupe_key ON invoices (dedupe_key);
CREATE INDEX ix_invoices_document_id ON invoices (document_id);
CREATE INDEX ix_invoices_invoice_date ON invoices (invoice_date);
CREATE INDEX ix_invoices_invoice_number ON invoices (invoice_number);
CREATE INDEX ix_invoices_review_status ON invoices (review_status);
CREATE INDEX ix_invoices_supplier_name ON invoices (supplier_name);

CREATE TABLE processing_events (
	id SERIAL NOT NULL, 
	document_id INTEGER NOT NULL, 
	stage VARCHAR(30) NOT NULL, 
	level VARCHAR(10) NOT NULL, 
	message TEXT NOT NULL, 
	duration_ms INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX ix_processing_events_document_id ON processing_events (document_id);

CREATE TABLE validation_issues (
	id SERIAL NOT NULL, 
	document_id INTEGER NOT NULL, 
	invoice_id INTEGER, 
	code VARCHAR(50) NOT NULL, 
	severity VARCHAR(10) NOT NULL, 
	field VARCHAR(40), 
	message TEXT NOT NULL, 
	resolved BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE, 
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE CASCADE
);

CREATE INDEX ix_validation_issues_code ON validation_issues (code);
CREATE INDEX ix_validation_issues_document_id ON validation_issues (document_id);
CREATE INDEX ix_validation_issues_invoice_id ON validation_issues (invoice_id);
