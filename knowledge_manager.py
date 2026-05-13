"""
Knowledge Manager - Document upload, embedding, and retrieval for agent knowledge.
Supports small documents (direct injection) and large documents (vector search via KNOWLEDGE: action).
"""

import os
import json
import uuid
import traceback
import numpy as np
from datetime import datetime

# Image file types that require OCR
IMAGE_TYPES = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'tif', 'webp'}


# --- OCR Functions ---

def is_tesseract_available():
    """Check if Tesseract OCR is installed and available."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_image(content_bytes):
    """Extract text from an image using Tesseract OCR."""
    import pytesseract
    from PIL import Image
    from io import BytesIO

    image = Image.open(BytesIO(content_bytes))
    text = pytesseract.image_to_string(image)
    return text.strip()


def ocr_pdf(content_bytes):
    """Extract text from a scanned PDF using Tesseract OCR (pdf → images → OCR)."""
    import pytesseract
    from pdf2image import convert_from_bytes

    images = convert_from_bytes(content_bytes)
    text_parts = []
    for i, image in enumerate(images):
        page_text = pytesseract.image_to_string(image)
        if page_text.strip():
            text_parts.append(page_text.strip())
        print(f"[KNOWLEDGE OCR] PDF page {i+1}/{len(images)} processed ({len(page_text)} chars)", flush=True)
    return '\n\n'.join(text_parts)


# --- Text Extraction ---

def extract_text(content_bytes, file_type):
    """Extract plain text from various file formats. OCR is applied automatically when needed."""
    file_type = file_type.lower()
    # Track whether OCR was used (returned via add_document metadata)
    ocr_used = False

    if file_type in ('txt', 'md', 'json', 'csv'):
        return content_bytes.decode('utf-8', errors='replace'), False

    elif file_type in IMAGE_TYPES:
        if not is_tesseract_available():
            raise ValueError("Tesseract OCR is not installed. Cannot process image files.")
        print(f"[KNOWLEDGE OCR] Image detected — applying OCR automatically...", flush=True)
        text = ocr_image(content_bytes)
        if text:
            print(f"[KNOWLEDGE OCR] Image OCR extracted {len(text)} chars", flush=True)
        return text, True

    elif file_type == 'pdf':
        # Try text extraction first
        try:
            from PyPDF2 import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(content_bytes))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            text = '\n'.join(text_parts)
        except Exception as e:
            text = ""
            print(f"[KNOWLEDGE] PyPDF2 text extraction failed: {e}", flush=True)

        # Auto-detect scanned PDF: if text extraction returned very little, fall back to OCR
        if len(text.strip()) < 50:
            if is_tesseract_available():
                print(f"[KNOWLEDGE OCR] PDF text extraction returned only {len(text.strip())} chars — scanned PDF detected. Applying OCR automatically...", flush=True)
                text = ocr_pdf(content_bytes)
                ocr_used = True
                print(f"[KNOWLEDGE OCR] PDF OCR extracted {len(text)} chars total", flush=True)
            else:
                print(f"[KNOWLEDGE] PDF appears scanned ({len(text.strip())} chars extracted) but Tesseract is not installed. Text may be incomplete.", flush=True)
        else:
            print(f"[KNOWLEDGE] PDF text extraction successful ({len(text.strip())} chars)", flush=True)

        return text, ocr_used

    elif file_type == 'docx':
        try:
            from docx import Document
            from io import BytesIO
            doc = Document(BytesIO(content_bytes))
            text_parts = [para.text for para in doc.paragraphs]
            return '\n'.join(text_parts), False
        except Exception as e:
            raise ValueError(f"Failed to extract text from DOCX: {e}")

    else:
        raise ValueError(f"Unsupported file type: {file_type}")


def chunk_text(text, chunk_size=1000, chunk_overlap=200):
    """Split text into overlapping chunks using langchain text splitters."""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", ". ", " ", ""]
        )
        return splitter.split_text(text)
    except ImportError:
        # Fallback: simple chunking
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start = end - chunk_overlap
        return chunks


# --- Embedding Functions ---

def create_embedding_function(provider, model_name, ollama_url=None, gemini_api_key=None):
    """
    Factory function returning an embedding callable.
    Returns: callable(texts: list[str]) -> list[list[float]]
    """
    if provider == 'ollama':
        import requests as req_lib

        def ollama_embed(texts):
            url = (ollama_url or 'http://localhost:11434').rstrip('/') + '/api/embeddings'
            embeddings = []
            for text in texts:
                resp = req_lib.post(url, json={'model': model_name, 'prompt': text}, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                embeddings.append(data['embedding'])
            return embeddings

        return ollama_embed

    elif provider == 'gemini':
        import google.generativeai as genai

        if gemini_api_key:
            genai.configure(api_key=gemini_api_key)

        def gemini_embed(texts):
            embeddings = []
            for text in texts:
                result = genai.embed_content(
                    model=model_name if model_name.startswith('models/') else f"models/{model_name}",
                    content=text
                )
                embeddings.append(result['embedding'])
            return embeddings

        return gemini_embed

    else:
        raise ValueError(f"Unsupported embedding provider: {provider}")


# --- Vector Store Wrappers ---

class ChromaStore:
    """ChromaDB-based vector store with persistent storage."""

    def __init__(self, embedding_fn, persist_dir):
        import chromadb
        self.embedding_fn = embedding_fn
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name="knowledge",
            metadata={"hnsw:space": "cosine"}
        )

    def add(self, doc_id, texts, metadatas):
        """Add text chunks with metadata to the collection."""
        if not texts:
            return
        embeddings = self.embedding_fn(texts)
        ids = [f"{doc_id}_chunk_{i}" for i in range(len(texts))]
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

    def query(self, query_text, top_k=5):
        """Query the collection and return matching chunks."""
        if self.collection.count() == 0:
            return []
        embeddings = self.embedding_fn([query_text])
        results = self.collection.query(
            query_embeddings=embeddings,
            n_results=min(top_k, self.collection.count())
        )
        output = []
        if results and results['documents']:
            for i, doc in enumerate(results['documents'][0]):
                meta = results['metadatas'][0][i] if results['metadatas'] else {}
                distance = results['distances'][0][i] if results['distances'] else 0
                output.append({
                    'text': doc,
                    'source': meta.get('source', 'unknown'),
                    'distance': distance
                })
        return output

    def delete(self, doc_id):
        """Delete all chunks for a document."""
        try:
            # Get all IDs matching this doc_id prefix
            all_items = self.collection.get(where={"doc_id": doc_id})
            if all_items and all_items['ids']:
                self.collection.delete(ids=all_items['ids'])
        except Exception:
            # Fallback: try deleting by ID pattern
            try:
                existing = self.collection.get()
                ids_to_delete = [id_ for id_ in existing['ids'] if id_.startswith(f"{doc_id}_chunk_")]
                if ids_to_delete:
                    self.collection.delete(ids=ids_to_delete)
            except Exception as e:
                print(f"ChromaStore delete error: {e}")

    def clear(self):
        """Clear all data from the collection."""
        try:
            self.client.delete_collection("knowledge")
            self.collection = self.client.get_or_create_collection(
                name="knowledge",
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:
            print(f"ChromaStore clear error: {e}")


class FAISSStore:
    """FAISS-based vector store with manual persistence."""

    def __init__(self, embedding_fn, persist_dir):
        import faiss
        self.faiss = faiss
        self.embedding_fn = embedding_fn
        self.persist_dir = persist_dir
        self.index_path = os.path.join(persist_dir, 'faiss_index.bin')
        self.metadata_path = os.path.join(persist_dir, 'faiss_metadata.json')
        self.index = None
        self.metadata = []  # list of {doc_id, chunk_index, source, text}
        self.dimension = None
        os.makedirs(persist_dir, exist_ok=True)
        self._load_if_exists()

    def _load_if_exists(self):
        """Load existing index and metadata from disk."""
        if os.path.exists(self.index_path) and os.path.exists(self.metadata_path):
            try:
                self.index = self.faiss.read_index(self.index_path)
                with open(self.metadata_path, 'r') as f:
                    self.metadata = json.load(f)
                if self.index.ntotal > 0:
                    self.dimension = self.index.d
            except Exception as e:
                print(f"FAISSStore load error: {e}. Starting fresh.")
                self.index = None
                self.metadata = []

    def save(self):
        """Save index and metadata to disk."""
        if self.index is not None:
            self.faiss.write_index(self.index, self.index_path)
            with open(self.metadata_path, 'w') as f:
                json.dump(self.metadata, f)

    def add(self, doc_id, texts, metadatas):
        """Add text chunks with metadata."""
        if not texts:
            return
        embeddings = self.embedding_fn(texts)
        embeddings_np = np.array(embeddings, dtype='float32')

        if self.index is None:
            self.dimension = embeddings_np.shape[1]
            self.index = self.faiss.IndexFlatIP(self.dimension)

        # Normalize for cosine similarity
        norms = np.linalg.norm(embeddings_np, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings_np = embeddings_np / norms

        self.index.add(embeddings_np)

        for i, text in enumerate(texts):
            self.metadata.append({
                'doc_id': doc_id,
                'chunk_index': i,
                'source': metadatas[i].get('source', 'unknown') if i < len(metadatas) else 'unknown',
                'text': text
            })

        self.save()

    def query(self, query_text, top_k=5):
        """Query the index and return matching chunks."""
        if self.index is None or self.index.ntotal == 0:
            return []

        embeddings = self.embedding_fn([query_text])
        query_np = np.array(embeddings, dtype='float32')

        # Normalize
        norm = np.linalg.norm(query_np, axis=1, keepdims=True)
        if norm[0][0] > 0:
            query_np = query_np / norm

        k = min(top_k, self.index.ntotal)
        distances, indices = self.index.search(query_np, k)

        output = []
        for i, idx in enumerate(indices[0]):
            if idx < len(self.metadata) and idx >= 0:
                meta = self.metadata[idx]
                output.append({
                    'text': meta['text'],
                    'source': meta['source'],
                    'distance': float(distances[0][i])
                })
        return output

    def delete(self, doc_id):
        """Delete all chunks for a document (rebuild index without them)."""
        remaining_meta = [m for m in self.metadata if m['doc_id'] != doc_id]
        if len(remaining_meta) == len(self.metadata):
            return  # Nothing to delete

        if not remaining_meta:
            self.clear()
            return

        # Rebuild index with remaining chunks
        texts = [m['text'] for m in remaining_meta]
        embeddings = self.embedding_fn(texts)
        embeddings_np = np.array(embeddings, dtype='float32')

        norms = np.linalg.norm(embeddings_np, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings_np = embeddings_np / norms

        self.index = self.faiss.IndexFlatIP(self.dimension)
        self.index.add(embeddings_np)
        self.metadata = remaining_meta
        self.save()

    def clear(self):
        """Clear all data."""
        if self.dimension:
            self.index = self.faiss.IndexFlatIP(self.dimension)
        else:
            self.index = None
        self.metadata = []
        # Remove files
        for path in [self.index_path, self.metadata_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def create_vector_store(store_type, embedding_fn, persist_dir):
    """Factory function returning a vector store instance."""
    os.makedirs(persist_dir, exist_ok=True)
    if store_type == 'chromadb':
        return ChromaStore(embedding_fn, persist_dir)
    elif store_type == 'faiss':
        return FAISSStore(embedding_fn, persist_dir)
    else:
        raise ValueError(f"Unsupported vector store type: {store_type}")


def _generate_doc_summary(text, llm, max_chars=900):
    """
    Generate a concise summary of a document for the knowledge index.
    If llm is None or fails, falls back to the first max_chars characters.
    The summary describes what the document contains and when an agent should search it.
    """
    if llm is not None:
        try:
            prompt = (
                "Write a concise summary (maximum 900 characters) of the following document for a knowledge base index. "
                "Describe: what topic the document covers, what specific information or data it contains, "
                "and when an AI agent should search it. Be specific about key topics, commands, settings, or data present. "
                "Plain text only, no markdown, no bullet points.\n\n"
                f"Document (first 8000 chars):\n{text[:8000]}\n\n"
                "Summary (max 900 characters):"
            )
            raw = llm.invoke(prompt)
            summary = raw.content if hasattr(raw, 'content') else str(raw)
            summary = summary.strip()[:max_chars]
            if summary:
                return summary
        except Exception as e:
            print(f"[KNOWLEDGE] LLM summary generation failed: {e}, using auto-summary")

    # Fallback: first max_chars chars trimmed to last sentence boundary
    snippet = text[:max_chars].strip()
    for sep in ['. ', '.\n', '\n\n', '\n']:
        pos = snippet.rfind(sep)
        if pos > max_chars // 2:
            snippet = snippet[:pos + 1].strip()
            break
    return snippet


# --- Main Knowledge Manager ---

class KnowledgeManager:
    """Manages document upload, storage, embedding, and retrieval."""

    ALLOWED_TYPES = {'txt', 'md', 'json', 'csv', 'pdf', 'docx'} | IMAGE_TYPES
    MAX_DOCUMENTS = 10   # Default — overridden by config.ini [Knowledge] max_documents
    MAX_FILE_SIZE_MB = 50  # Default — overridden by config.ini [Knowledge] max_file_size_mb

    def __init__(self, storage_dir, embedding_provider, embedding_model,
                 vector_store_type, summarization_threshold,
                 ollama_url=None, gemini_api_key=None):
        """
        Args:
            storage_dir: Path to store documents and vector DB (KEYS_DIR/knowledge/)
            embedding_provider: 'ollama' or 'gemini'
            embedding_model: Model name (e.g., 'nomic-embed-text', 'models/embedding-001')
            vector_store_type: 'chromadb' or 'faiss'
            summarization_threshold: From config, used for small doc threshold (10%)
            ollama_url: Ollama API URL (for embedding)
            gemini_api_key: Gemini API key (for embedding)
        """
        self.storage_dir = storage_dir
        self.docs_dir = os.path.join(storage_dir, 'documents')
        self.vector_dir = os.path.join(storage_dir, 'vector_store')
        self.metadata_path = os.path.join(storage_dir, 'documents_meta.json')
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.vector_store_type = vector_store_type
        self.summarization_threshold = summarization_threshold
        self.small_doc_threshold = int(summarization_threshold * 0.1)

        # Read configurable limits from config.ini
        try:
            from config import get_config as _get_config
            _cfg = _get_config()
            self.MAX_DOCUMENTS = _cfg.getint('Knowledge', 'max_documents', fallback=10)
            self.MAX_FILE_SIZE_MB = _cfg.getint('Knowledge', 'max_file_size_mb', fallback=50)
        except Exception:
            self.MAX_DOCUMENTS = 10
            self.MAX_FILE_SIZE_MB = 50

        os.makedirs(self.docs_dir, exist_ok=True)
        os.makedirs(self.vector_dir, exist_ok=True)

        # Load document metadata
        self.documents = self._load_metadata()

        # Create embedding function
        self.embedding_fn = create_embedding_function(
            embedding_provider, embedding_model,
            ollama_url=ollama_url, gemini_api_key=gemini_api_key
        )

        # Create vector store
        self.vector_store = create_vector_store(
            vector_store_type, self.embedding_fn, self.vector_dir
        )

        print(f"KnowledgeManager initialized: {len(self.documents)} documents, "
              f"provider={embedding_provider}, model={embedding_model}, "
              f"store={vector_store_type}, small_threshold={self.small_doc_threshold} chars")

    def _load_metadata(self):
        """Load document metadata from disk."""
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, 'r') as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_metadata(self):
        """Save document metadata to disk."""
        with open(self.metadata_path, 'w') as f:
            json.dump(self.documents, f, indent=2)

    def add_document(self, filename, content_bytes, file_type, source_label='USER', extra_metadata=None, llm=None):
        """
        Process and store a document. All documents are vectorized (embedded mode).
        A summary is generated via LLM if provided, otherwise the first 900 chars are used.
        Args:
            source_label: 'USER' (uploaded manually) or 'WEB' (from web search module)
            extra_metadata: Optional dict with additional metadata (url, search_query, etc.)
            llm: Optional LLM instance for generating a semantic summary of the document
        Returns: {id, filename, type, size, mode, chunk_count, ocr_used, source, summary}
        """
        if len(self.documents) >= self.MAX_DOCUMENTS:
            raise ValueError(f"Maximum document limit ({self.MAX_DOCUMENTS}) reached.")

        max_bytes = self.MAX_FILE_SIZE_MB * 1024 * 1024
        if len(content_bytes) > max_bytes:
            raise ValueError(f"File too large: {len(content_bytes) / (1024 * 1024):.1f} MB exceeds limit of {self.MAX_FILE_SIZE_MB} MB.")

        if file_type not in self.ALLOWED_TYPES:
            raise ValueError(f"Unsupported file type: {file_type}. Allowed: {', '.join(self.ALLOWED_TYPES)}")

        # Extract text (OCR applied automatically for images and scanned PDFs)
        text, ocr_used = extract_text(content_bytes, file_type)
        if not text.strip():
            raise ValueError("Document is empty or text could not be extracted.")

        doc_id = str(uuid.uuid4())[:8]
        text_size = len(text)

        # All documents are embedded (vectorized) regardless of size.
        # The 'direct' injection mode is no longer used — summaries replace full-content injection.
        chunks = chunk_text(text)
        chunk_count = len(chunks)
        metadatas = [{'source': filename, 'doc_id': doc_id, 'chunk_index': i} for i in range(len(chunks))]
        self.vector_store.add(doc_id, chunks, metadatas)

        # Generate summary via LLM or auto-fallback (first ~900 chars)
        summary = _generate_doc_summary(text, llm)

        # Save document text to disk
        doc_text_path = os.path.join(self.docs_dir, f"{doc_id}.txt")
        with open(doc_text_path, 'w', encoding='utf-8') as f:
            f.write(text)

        # Save metadata
        doc_meta = {
            'id': doc_id,
            'filename': filename,
            'type': file_type,
            'size': text_size,
            'mode': 'embedded',
            'chunk_count': chunk_count,
            'ocr_used': ocr_used,
            'source': source_label,
            'summary': summary,
            'uploaded_at': datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        }
        # Merge extra metadata if provided — reserved keys are protected from overwrite
        if extra_metadata:
            RESERVED_KEYS = {'id', 'filename', 'type', 'size', 'mode', 'chunk_count', 'ocr_used', 'uploaded_at', 'summary'}
            safe_extra = {k: v for k, v in extra_metadata.items() if k not in RESERVED_KEYS}
            doc_meta.update(safe_extra)
        self.documents.append(doc_meta)
        self._save_metadata()

        summary_src = 'LLM' if llm else 'auto'
        print(f"Knowledge document added: {filename} (embedded, {text_size} chars, {chunk_count} chunks, summary={summary_src})")
        return doc_meta

    def generate_missing_summaries(self, llm):
        """
        Migration helper: generates summaries and re-embeds any documents that were stored
        in the old 'direct' mode (not vectorized). Safe to call multiple times — skips
        documents that already have a summary and are already embedded.
        Returns the number of documents updated.
        """
        updated = 0
        for doc in self.documents:
            doc_text_path = os.path.join(self.docs_dir, f"{doc['id']}.txt")
            if not os.path.exists(doc_text_path):
                continue

            try:
                with open(doc_text_path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except Exception as e:
                print(f"[KNOWLEDGE] Migration: cannot read {doc['filename']}: {e}")
                continue

            changed = False

            # Re-embed if still in direct mode (not vectorized)
            if doc.get('mode') == 'direct':
                try:
                    chunks = chunk_text(text)
                    metadatas = [{'source': doc['filename'], 'doc_id': doc['id'], 'chunk_index': i}
                                 for i in range(len(chunks))]
                    self.vector_store.add(doc['id'], chunks, metadatas)
                    doc['mode'] = 'embedded'
                    doc['chunk_count'] = len(chunks)
                    changed = True
                    print(f"[KNOWLEDGE] Migration: re-embedded {doc['filename']} ({len(chunks)} chunks)")
                except Exception as e:
                    print(f"[KNOWLEDGE] Migration: re-embedding failed for {doc['filename']}: {e}")

            # Generate summary if missing
            if not doc.get('summary'):
                doc['summary'] = _generate_doc_summary(text, llm)
                changed = True
                src = 'LLM' if llm else 'auto'
                print(f"[KNOWLEDGE] Migration: generated {src} summary for {doc['filename']}")

            if changed:
                updated += 1

        if updated > 0:
            self._save_metadata()
            print(f"[KNOWLEDGE] Migration complete: {updated} document(s) updated.")

        return updated

    def get_limit_status(self) -> dict:
        """Return current document count vs limit, with warning flags."""
        count = len(self.documents)
        max_docs = self.MAX_DOCUMENTS
        return {
            'count': count,
            'max': max_docs,
            'at_limit': count >= max_docs,
            'near_limit': count >= max_docs - 1 and count < max_docs,  # 1 slot left
        }

    def remove_document(self, doc_id):
        """Remove a document and its embeddings from the store."""
        doc = next((d for d in self.documents if d['id'] == doc_id), None)
        if not doc:
            return False

        # Remove from vector store if embedded
        if doc['mode'] == 'embedded':
            try:
                self.vector_store.delete(doc_id)
            except Exception as e:
                print(f"Error removing embeddings for {doc_id}: {e}")

        # Remove text file
        doc_text_path = os.path.join(self.docs_dir, f"{doc_id}.txt")
        if os.path.exists(doc_text_path):
            try:
                os.remove(doc_text_path)
            except OSError:
                pass

        # Remove from metadata
        self.documents = [d for d in self.documents if d['id'] != doc_id]
        self._save_metadata()

        print(f"Knowledge document removed: {doc['filename']} ({doc_id})")
        return True

    def get_documents_list(self):
        """Return list of all uploaded documents with metadata."""
        return self.documents

    def get_document_text(self, doc_id):
        """Return the raw text content of a document by ID."""
        doc = next((d for d in self.documents if d['id'] == doc_id), None)
        if not doc:
            return None
        doc_text_path = os.path.join(self.docs_dir, f"{doc_id}.txt")
        if os.path.exists(doc_text_path):
            try:
                with open(doc_text_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading document {doc_id}: {e}")
                return None
        return None

    def get_direct_inject_context(self):
        """
        Returns a knowledge index with one summary per document.
        Full document content is NOT injected — the agent must use KNOWLEDGE: <query>
        to retrieve relevant chunks from any document listed here.
        """
        if not self.documents:
            return ""

        entries = []
        for doc in self.documents:
            summary = doc.get('summary') or doc.get('filename', '')
            size_info = f"{doc['size']} chars"
            if doc.get('chunk_count'):
                size_info += f", {doc['chunk_count']} chunks"
            entries.append(
                f"  [{doc['filename']}] ({size_info})\n"
                f"  {summary}"
            )

        header = f"=== KNOWLEDGE BASE ({len(self.documents)} document(s)) ===\n"
        header += "Use KNOWLEDGE: <query> to retrieve relevant content from any document below.\n"
        body = "\n\n".join(entries)
        footer = "\n=== END KNOWLEDGE BASE ==="

        return header + "\n" + body + footer

    def has_documents(self):
        """Return True if any documents are uploaded."""
        return len(self.documents) > 0

    def has_embedded_documents(self):
        """Return True if any embedded (large) documents exist."""
        return any(d['mode'] == 'embedded' for d in self.documents)

    def query(self, query_text, top_k=5):
        """
        Search embedded documents for relevant chunks.
        Returns formatted string of matching chunks with source attribution.
        """
        try:
            results = self.vector_store.query(query_text, top_k=top_k)
        except Exception as e:
            return f"Knowledge search error: {e}"

        if not results:
            return "No relevant knowledge found for this query."

        parts = []
        for i, result in enumerate(results, 1):
            source = result.get('source', 'unknown')
            text = result.get('text', '')
            parts.append(f"[Source: {source}] (Result {i})\n{text}")

        return "\n\n---\n\n".join(parts)

    def get_document_count(self):
        """Return number of uploaded documents."""
        return len(self.documents)

    def clear_by_source(self, source_label):
        """Remove all documents with a specific source label (e.g., 'WEB')."""
        docs_to_remove = [d for d in self.documents if d.get('source', 'USER') == source_label]
        if not docs_to_remove:
            return 0

        count = 0
        for doc in docs_to_remove:
            if doc['mode'] == 'embedded':
                try:
                    self.vector_store.delete(doc['id'])
                except Exception as e:
                    print(f"Error removing embeddings for {doc['id']}: {e}")

            doc_text_path = os.path.join(self.docs_dir, f"{doc['id']}.txt")
            if os.path.exists(doc_text_path):
                try:
                    os.remove(doc_text_path)
                except OSError:
                    pass
            count += 1

        self.documents = [d for d in self.documents if d.get('source', 'USER') != source_label]
        self._save_metadata()
        print(f"Cleared {count} knowledge documents with source={source_label}")
        return count

    def get_documents_by_source(self, source_label):
        """Return list of documents filtered by source label."""
        return [d for d in self.documents if d.get('source', 'USER') == source_label]

    def clear_all(self):
        """Remove all documents and clear vector store."""
        # Clear vector store
        try:
            self.vector_store.clear()
        except Exception as e:
            print(f"Error clearing vector store: {e}")

        # Remove all document text files
        for doc in self.documents:
            doc_text_path = os.path.join(self.docs_dir, f"{doc['id']}.txt")
            if os.path.exists(doc_text_path):
                try:
                    os.remove(doc_text_path)
                except OSError:
                    pass

        self.documents = []
        self._save_metadata()
        print("All knowledge documents cleared.")
