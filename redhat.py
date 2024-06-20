from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import os
import httpx
import faiss
import pickle
from schemas import QueryModel
from langchain.embeddings import CacheBackedEmbeddings
from langchain.storage import LocalFileStore
from langchain_community.embeddings.huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders.unstructured import UnstructuredFileLoader
from langchain_community.vectorstores.faiss import FAISS

router = APIRouter(
    prefix="/redhat",
    tags=["redhat"]
)

# 필수 디렉토리 생성
os.makedirs(".cache/embeddings/redhat", exist_ok=True)
os.makedirs(".cache/files/redhat", exist_ok=True)
timeout = httpx.Timeout(120.0, read=120.0)
OLLAMA_URL = "http://20.39.201.16:11434/api/generate"

RAG_PROMPT_TEMPLATE = """당신은 질문에 친절하게 답변하는 REDHAT AI입니다. 주어진 문맥을 사용하여 질문에 답변하세요. 답을 모른다면 모른다고 답변하세요.
Question: {question}
Context: {context}
Answer:"""

# 전역 retriever 변수 선언
retriever = None

def save_faiss_index(vectorstore, dir_path):
    faiss.write_index(vectorstore.index, os.path.join(dir_path, "index.faiss"))
    with open(os.path.join(dir_path, "index.pkl"), "wb") as f:
        pickle.dump(vectorstore.index_to_docstore_id, f)
        pickle.dump(vectorstore.docstore, f)
        pickle.dump(vectorstore.embeddings, f)

def load_faiss_index(dir_path, embeddings):
    index = faiss.read_index(os.path.join(dir_path, "index.faiss"))
    with open(os.path.join(dir_path, "index.pkl"), "rb") as f:
        index_to_docstore_id = pickle.load(f)
        docstore = pickle.load(f)
        # embeddings 인자를 제거하고 필요한 인자만 전달
        vectorstore = FAISS(
            index=index,
            docstore=docstore,
            index_to_docstore_id=index_to_docstore_id,
            embedding_function=embeddings.embed_query  # 여기서 embedding_function을 전달
        )
    return vectorstore

def embed_file(file_path):
    cache_dir_path = f"./.cache/embeddings/redhat/{os.path.basename(file_path)}"

    # 이미 임베딩이 되어있는지 확인
    if os.path.exists(os.path.join(cache_dir_path, "index.faiss")):
        # 이미 임베딩된 경우, 기존 임베딩을 로드
        embeddings = HuggingFaceEmbeddings(
            model_name="/home/osslab/mumul_dev/bge-m3",
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True}
        )
        cached_embeddings = CacheBackedEmbeddings.from_bytes_store(embeddings, LocalFileStore(cache_dir_path))
        vectorstore = load_faiss_index(cache_dir_path, cached_embeddings)
        retriever = vectorstore.as_retriever()
        return retriever

    # 임베딩되지 않은 경우, 새로운 임베딩 생성
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", "(?<=\. )", " ", ""],
        length_function=len,
    )
    loader = UnstructuredFileLoader(file_path)
    docs = loader.load_and_split(text_splitter=text_splitter)

    embeddings = HuggingFaceEmbeddings(
        model_name="/home/osslab/mumul_dev/bge-m3",
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    cached_embeddings = CacheBackedEmbeddings.from_bytes_store(embeddings, LocalFileStore(cache_dir_path))
    vectorstore = FAISS.from_documents(docs, embedding=cached_embeddings)
    os.makedirs(cache_dir_path, exist_ok=True)
    save_faiss_index(vectorstore, cache_dir_path)  # 벡터스토어를 로컬에 저장
    retriever = vectorstore.as_retriever()
    return retriever

async def query_ollama(inputs: str) -> dict:
    try:
        payload = {
            "model": "EEVE-Korean-10.8B:latest",
            "prompt": inputs,
            "stream": False
        }
        headers = {
            'Content-Type': 'application/json'
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(OLLAMA_URL, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            return result['response']
    except httpx.HTTPStatusError as http_error:
        raise HTTPException(status_code=http_error.response.status_code, detail=http_error.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def process_query(inputs: str) -> str:
    context_docs = retriever.invoke(inputs)
    context = "\n\n".join(doc.page_content for doc in context_docs)
    prompt = RAG_PROMPT_TEMPLATE.format(question=inputs, context=context)
    answer = await query_ollama(prompt)
    return answer

def load_and_embed_pdfs():
    global retriever  # 전역 변수를 사용하도록 지정
    pdf_files = ["/home/osslab/red_hat_enterprise_linux-7-system_administrators_guide-ko-kr.pdf"]  # 여기에 PDF 파일 경로를 추가하세요
    for pdf_file in pdf_files:
        retriever = embed_file(pdf_file)

@router.on_event("startup")
async def on_startup():
    load_and_embed_pdfs()

@router.post("/query")
async def query_doc(query: QueryModel):
    try:
        answer = await process_query(query.inputs)
        return JSONResponse(content=answer)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
