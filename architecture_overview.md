# LexAI Architecture Overview

## 1. Project Summary

LexAI is a legal question-answering system built around a Retrieval-Augmented Generation (RAG) pipeline. The project ingests legal documents, chunks them into smaller retrievable units, stores them in a hybrid retrieval index, retrieves relevant chunks for a user query, reranks them, and then generates an answer grounded in the retrieved evidence.

The project is designed to work locally and to be explainable, modular, and evaluation-friendly. The main goal is not only to generate answers, but also to make retrieval and reasoning transparent and measurable.

---

## 2. Core Architecture

The system can be broken into five major layers:

1. Document ingestion layer
2. Chunking and preprocessing layer
3. Retrieval layer
4. Reranking and context assembly layer
5. Answer generation layer

Each layer is implemented as a separate module so the pipeline can be debugged, benchmarked, and improved independently.

### High-level flow

1. Legal documents are loaded from the data directory.
2. The documents are segmented into semantically meaningful chunks.
3. Chunks are embedded and stored in the vector index.
4. A user query is processed through the retrieval pipeline.
5. Relevant chunks are retrieved from dense and sparse indexes.
6. The candidate chunks are reranked.
7. The top chunks are passed into the LLM to generate an answer.
8. The system stores evaluation and benchmark results for continuous improvement.

---

## 3. Project Structure

### Backend

The backend contains the core RAG implementation.

- app/main.py
  - Entry point for the FastAPI application.
- app/api/routes.py
  - API routes for querying the system and serving responses.
- app/config/settings.py
  - Central configuration for chunking, retrieval, reranking, and model settings.
- app/models/schemas.py
  - Pydantic models for the request and response objects.
- app/services/
  - Contains the retrieval, memory, QA, reranking, vector store, and provider logic.

### Ingestion

- ingestion/chunker.py
  - Implements semantic chunking.
- ingestion/ingest.py
  - Runs the document ingestion pipeline and builds the retrieval index.
- ingestion/metadata.py
  - Supports metadata extraction and document enrichment.

### Evaluation

- evaluation/run_benchmark.py
  - Runs the benchmark pipeline for retrieval evaluation.
- evaluation/build_golden_dataset.py
  - Builds or prepares evaluation data.
- evaluation/run_ragas.py
  - Runs retrieval and generation quality checks.

### Frontend

- frontend/
  - Vite-based UI that sends user queries to the backend and displays responses, citations, and chat history.

---

## 4. Main Features Implemented

### 4.1 Local-first RAG pipeline

Why it was chosen:
- The project needed to be usable without depending heavily on cloud services.
- Local development and experimentation were prioritized.
- This makes the project easier to run, debug, and demo.

How it is implemented:
- The backend uses a local FastAPI application.
- Queries are processed through a local retrieval pipeline.
- The system can work with local or fallback embedding behavior to avoid network dependency.

Why this is important:
- It reduces latency.
- It avoids dependence on third-party API quotas during development.
- It makes the system more robust for experimentation.

---

### 4.2 Hybrid retrieval with FAISS and BM25

Why it was chosen:
- Dense retrieval alone can miss keyword-heavy or exact-match information.
- Sparse retrieval alone can miss semantic similarity.
- Hybrid retrieval provides a more balanced approach.

How it is implemented:
- Dense retrieval is performed using a FAISS-based vector store.
- Sparse retrieval is performed using BM25.
- The results from both methods are fused using Reciprocal Rank Fusion (RRF).

Why this is important:
- It improves recall.
- It makes retrieval more robust across different query styles.
- It is a very common and practical retrieval strategy for real RAG systems.

---

### 4.3 Reranking layer

Why it was chosen:
- The initial retrieval step often returns many relevant candidates, but not always in the best order.
- A reranker helps place the most relevant evidence nearer the top.

How it is implemented:
- The retrieval stage first collects a larger candidate set.
- That set is passed through a reranking step.
- The top-ranked evidence is then used for answer generation.

Why this is important:
- It improves ranking quality.
- It helps the LLM receive the best possible context.
- It directly improves answer relevance.

---

### 4.4 Semantic chunking

Why it was chosen:
- Raw documents are too large to retrieve effectively at the chunk level.
- Very large chunks hurt precision and make retrieval noisy.
- Smaller, coherent chunks improve retrieval quality.

How it is implemented:
- The ingestion pipeline splits documents into chunks using semantic chunking logic.
- The chunker uses configurable minimum and maximum length constraints.
- Overlap is used to preserve context between adjacent chunks.

Why this is important:
- It improves the granularity of retrieval.
- It helps the system match the user’s question against smaller, precise evidence units.
- It reduces context dilution.

---

### 4.5 Fallback embedding strategy

Why it was chosen:
- Using external embedding APIs can introduce quota and rate-limit issues.
- A fallback strategy keeps the system operational during development and local runs.

How it is implemented:
- The embedding service first attempts to use the configured provider if available.
- If that path is unavailable or fails, the system falls back to a deterministic local embedding implementation.
- This avoids breaking the workflow when the external service is unavailable.

Why this is important:
- It improves reliability.
- It supports offline and local experimentation.
- It makes the system much easier to demonstrate and test.

---

### 4.6 Evaluation and benchmark pipeline

Why it was chosen:
- Retrieval quality should be measured, not assumed.
- A benchmark helps compare different retrieval strategies objectively.

How it is implemented:
- The project includes a benchmark runner that loads evaluation queries and documents.
- It computes metrics such as precision, recall, and nDCG.
- It evaluates baseline FAISS-only retrieval, hybrid retrieval, and reranked retrieval.

Why this is important:
- It provides evidence-based improvement.
- It supports ablation studies.
- It shows whether a change actually improves system quality.

---

### 4.7 Golden dataset / evaluation dataset support

Why it was chosen:
- A good RAG system needs curated test data to measure quality.
- The benchmark should not rely only on intuition.

How it is implemented:
- The evaluation module includes support for creating and loading a golden dataset.
- It helps compare expected retrieval results against actual results.

Why this is important:
- It makes the system more research-oriented.
- It supports repeatable evaluation.
- It is useful for demonstrating rigor in interviews and portfolio work.

---

### 4.8 FastAPI backend with a frontend chat interface

Why it was chosen:
- A simple web interface makes the project easier to demo.
- A backend API makes the system modular and easier to extend.

How it is implemented:
- The backend exposes API routes for chat and querying.
- The frontend sends requests to the backend and renders the conversation.
- The UI includes chat history, citations, and the user input experience.

Why this is important:
- It turns the research system into a usable application.
- It helps stakeholders understand the feature in action.
- It makes the project more interview-friendly.

---

## 5. Why These Choices Were Made

The architecture was designed around a balance of four principles:

1. Accuracy
   - Retrieval should find relevant evidence.
   - Reranking improves the order of candidates.

2. Reliability
   - The system should keep working even when some APIs or services are unavailable.
   - Fallback behavior helps with this.

3. Modularity
   - Each major part is isolated so it can be improved independently.
   - This helps during development, debugging, and interviews.

4. Measurability
   - The project includes benchmarking and evaluation support.
   - This makes it easier to justify design choices.

---

## 6. How the System Works End-to-End

### Step 1: Document ingestion
- Source documents are discovered.
- They are parsed and cleaned.
- The ingestion module prepares them for chunking.

### Step 2: Chunking
- Large documents are split into smaller chunks.
- Chunk overlap and length constraints preserve context.

### Step 3: Indexing
- Each chunk is embedded.
- Dense vectors are stored in FAISS.
- Sparse tokens are stored for BM25 search.

### Step 4: Query handling
- A user asks a question.
- The system sends the query through the hybrid retrieval pipeline.

### Step 5: Retrieval and reranking
- Dense retrieval finds semantically similar chunks.
- Sparse retrieval finds keyword-based matches.
- The results are combined and reranked.

### Step 6: Answer generation
- The top evidence is passed into the LLM.
- The LLM uses the retrieved context to answer the question.
- The answer is returned to the frontend and displayed to the user.

---

## 7. Strengths of the Project

- Strong demonstration of RAG concepts
- Clear modular architecture
- Hybrid retrieval strategy
- Evaluation-driven development
- Local-friendly and practical implementation
- Good for interviews and portfolio presentation

---

## 8. Limitations and Future Improvements

Even though the project is strong for an interview/demo, there is still room for improvement:

- Better chunk quality
- Better query expansion
- Better reranking quality
- Higher-quality embeddings
- More robust document cleaning and metadata extraction
- More advanced evaluation beyond basic retrieval metrics

These are the natural next steps if the project were to become more production-ready.

---

## 9. Interview-Ready Summary

You can describe the project like this:

“I built a legal RAG system that ingests documents, chunks them semantically, indexes them using hybrid retrieval, reranks the candidates, and generates answers grounded in retrieved evidence. I also built an evaluation pipeline to benchmark retrieval quality and compare different retrieval strategies. The project is modular, local-friendly, and designed to demonstrate both engineering and AI retrieval concepts.”

---

## 10. Interview Questions and Suggested Answers

These answers are written in a more natural, first-person style so you can use them directly in an interview.

### 1. What is RAG?
My answer: “I’d explain RAG as a way of making an LLM answer questions using external evidence instead of relying only on what it already learned during training. In this project, the system retrieves relevant legal document chunks first and then uses them to generate a grounded answer.”

### 2. Why did you choose a RAG architecture for this project?
My answer: “I chose RAG because the system needed to answer questions from a specific domain, which in this case is legal documents. A traditional LLM alone would not be reliable enough for that, so I wanted the answer generation step to be grounded in retrieved evidence.”

### 3. What is the main difference between dense and sparse retrieval?
My answer: “Dense retrieval is semantic; it uses vector similarity to find meaning-based matches. Sparse retrieval is keyword-based and is better for exact term matching. I used both because they complement each other well.”

### 4. Why did you use hybrid retrieval?
My answer: “I used hybrid retrieval because I wanted the best of both worlds. Dense retrieval helps with semantic understanding, while BM25 helps with lexical precision. Together, they improve recall and make retrieval more robust.”

### 5. What is FAISS used for here?
My answer: “FAISS is used to index and search dense embeddings efficiently. It helps the system quickly find semantically similar chunks when a user asks a question.”

### 6. What is BM25?
My answer: “BM25 is the sparse retrieval component in the project. It helps the system match documents based on important keywords, which is especially useful when the query contains very specific legal terms.”

### 7. What is the role of reranking?
My answer: “Reranking is important because the first retrieval pass often brings in a lot of candidates, but not always in the best order. I use reranking to push the most relevant evidence higher so the final answer is better grounded.”

### 8. Why is chunking important in RAG?
My answer: “Chunking is critical because large documents are too noisy and too broad to retrieve effectively. If I pass the entire document as one block, the retrieval step becomes less precise. Smaller, meaningful chunks make the system more accurate.”

### 9. What does semantic chunking mean?
My answer: “Semantic chunking means splitting the document into meaningful units rather than just cutting it into fixed-size pieces. I wanted the chunks to preserve context and be more coherent for retrieval.”

### 10. Why did you implement a fallback embedding strategy?
My answer: “I implemented a fallback strategy because external embedding services can be unstable or rate-limited. I wanted the project to remain usable even when the preferred path is unavailable, so the system still runs locally and continues to work.”

### 11. What is the purpose of the benchmark pipeline?
My answer: “The benchmark pipeline is there to measure whether the retrieval improvements are actually helping. I did not want to rely on intuition alone, so I built an evaluation flow that compares different retrieval setups objectively.”

### 12. What metrics did you use for evaluation?
My answer: “I used metrics like Precision@5, Recall@5, and nDCG@10. Those metrics are helpful because they tell me not just whether relevant documents were found, but also whether they were ranked well.”

### 13. What does Precision@5 measure?
My answer: “Precision@5 tells me how many of the top 5 retrieved results are actually relevant. It is a good indicator of how focused the retrieval is.”

### 14. What does Recall@5 measure?
My answer: “Recall@5 tells me whether the relevant evidence appears in the top 5 retrieved results. It is useful when I want to understand whether the system is finding the right information at all.”

### 15. What does nDCG measure?
My answer: “nDCG measures ranking quality by rewarding highly relevant results that appear earlier in the list. That is important because in retrieval, the order of results really matters.”

### 16. How did you choose the retrieval configuration?
My answer: “I compared multiple configurations, including FAISS-only, hybrid retrieval, and hybrid retrieval with reranking. I chose the setup based on benchmark results rather than just assumptions.”

### 17. What is the advantage of a modular architecture?
My answer: “A modular architecture makes the system easier to debug, test, and improve. If one part is weak, I can work on that part without rewriting the whole project.”

### 18. Why is the frontend important for this project?
My answer: “The frontend is important because it turns the technical pipeline into something usable and demonstrable. It helps users interact with the system and makes the workflow easy to understand.”

### 19. What were the main challenges in the project?
My answer: “The main challenges were retrieval quality, chunk quality, and making sure the evaluation flow was reliable. I also had to deal with service dependencies and make the system robust enough to run locally.”

### 20. What would you improve next if you had more time?
My answer: “If I had more time, I would improve the chunking strategy further, add better query expansion, and try stronger reranking and embedding approaches. Those are the areas that usually give the biggest gains in RAG systems.”

### 21. How would you explain your project in one minute?
My answer: “I would say I built a legal RAG system that ingests documents, retrieves relevant evidence, reranks it, and generates grounded answers. I also built an evaluation framework to measure how well the retrieval pipeline performs.”

### 22. What is the difference between retrieval and generation in this project?
My answer: “Retrieval is about finding the right evidence, while generation is about turning that evidence into a useful answer. In my project, both steps are important, but retrieval is what determines how grounded the final answer will be.”

### 23. Why is grounding important in RAG?
My answer: “Grounding is important because it reduces hallucination and makes the answer more trustworthy. If the model is citing retrieved context, the response is much more reliable than if it were relying only on memory.”

### 24. What role does the API play in the architecture?
My answer: “The API acts as the interface between the frontend and the backend. It makes the system modular and allows the application to be extended easily later.”

### 25. Why are citations valuable in a RAG system?
My answer: “Citations are valuable because they let the user verify the answer and see the evidence behind it. That makes the system more transparent and more useful in real-world use cases.”

### 26. How does this project demonstrate machine learning engineering skills?
My answer: “I think this project shows a strong mix of engineering and ML work. It includes model integration, vector search, system design, evaluation, and application development, so it demonstrates that I can build and improve an AI system end to end.”

### 27. What is the biggest limitation of the current system?
My answer: “The biggest limitation is retrieval quality. The system works, but there is still room to improve how relevant chunks are selected and ranked, because that directly affects the final answer quality.”

### 28. How would you scale this system later?
My answer: “I would scale it by improving the indexing layer, using a more scalable vector store, and possibly introducing more advanced retrieval and ranking services. I would also optimize the ingestion pipeline so larger corpora can be handled more efficiently.”

### 29. Why is evaluation important for AI systems?
My answer: “Evaluation is important because it tells me whether my changes are actually improving the system. Without evaluation, I would just be guessing, and that is not a good way to build reliable AI applications.”

### 30. How would you present this project in an interview?
My answer: “I would present it as a practical RAG project that combines retrieval, reranking, evaluation, backend engineering, and a simple UI. I would also highlight that I focused on building the pipeline iteratively and improving it based on measurable results.”

---

## 11. Closing Note

This project is a strong interview project because it combines:
- backend engineering,
- AI application design,
- retrieval systems,
- benchmarking,
- and frontend integration.

It shows that you understand not only how to build an AI app, but also how to improve it systematically and explain the decisions behind it.
