import asyncio
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from bs4.filter import SoupStrainer

from app.utils.text_process import preprocess

from .search_service import SearchService


class RetrievalService:
    def __init__(self):
        self.search = SearchService()

    def replace_special_words(self, text: str) -> str:
        return text

    def preprocess_pipeline(self, texts: list[str], *args, **kwargs) -> list[str]:
        texts = [text.lower() for text in texts]
        texts = [self.replace_special_words(text) for text in texts]
        return texts

    @staticmethod
    def build_tag_index(soup: Any) -> Any:
        """Index all <a> tags by their 'name' attribute for O(1) access."""
        index = defaultdict(list)
        for tag in soup.find_all("a", attrs={"name": True}):
            index[tag["name"]].append(tag)
        return index

    @staticmethod
    def find_p_tags_until_next_dieu_optimized(soup: BeautifulSoup, number: int) -> list[Tag]:
        if not soup:
            return []

        # Simple searching is fast enough on a Strainer-reduced soup
        start_pattern = re.compile(rf"Điều {number}\.")
        next_dieu_pattern = re.compile(r"Điều \d+\.")

        all_p_tags = soup.find_all("p")

        results = []
        is_capturing = False
        for p_tag in all_p_tags:
            tag_text = p_tag.get_text()
            if is_capturing:
                if next_dieu_pattern.search(tag_text):
                    break
                else:
                    results.append(p_tag)
            elif start_pattern.search(tag_text):
                is_capturing = True
                results.append(p_tag)
        return results

    @staticmethod
    def filter_and_transform_a_tags_indexed(source_a_tags: list[Tag], tag_index: dict) -> Any:
        """
        Uses a dictionary lookup (O(1)) instead of scanning the whole DOM (O(N)).
        """
        based_tag = set()
        for a_tag in source_a_tags:
            original_name = a_tag.get("name")
            if original_name and original_name.startswith("x"):
                parts = original_name.split("_")
                if len(parts) == 2:
                    base_name, _ = parts
                    based_tag.add(base_name)

        unique_based_tag = list(based_tag)
        pattern_dieu = re.compile(r"^\s*Điều\s+(\d+)")
        found_article_numbers = []

        for name in unique_based_tag:
            # 1. Retrieve candidate tags from Index
            candidate_tags = []

            for key, tags in tag_index.items():
                if key.startswith(name):
                    candidate_tags.extend(tags)

            if not candidate_tags:
                continue

            # Logic: if >= 2, take index 1, else index 0
            a_tag = candidate_tags[1] if len(candidate_tags) >= 2 else candidate_tags[0]

            text_2_check = a_tag.get_text().strip()
            match = pattern_dieu.match(text_2_check)
            if match:
                found_article_numbers.append(str(match.group(1)))

            parent_p = a_tag.find_parent("p")
            if not parent_p:
                continue

            previous_p_tags = parent_p.find_all_previous("p")
            for prev_p in previous_p_tags:
                text_to_check = prev_p.get_text().strip()
                match = pattern_dieu.match(text_to_check)
                if match:
                    found_article_numbers.append(str(match.group(1)))
                    break

        return found_article_numbers

    @staticmethod
    def extract_a_tags_from_p_list(p_tags: list[Tag]) -> list[Tag]:
        all_a_tags = []
        for p_tag in p_tags:
            all_a_tags.extend(p_tag.find_all("a"))
        return all_a_tags

    @staticmethod
    def process_stage_group(stage_group_data: Any, last_law_id: int, init_document_name: str) -> Any:
        """
        This function runs in a separate process.
        It handles ONE item from 'all_stages' (one law, multiple articles).
        """
        if stage_group_data["stages"] == []:
            return {
                "results": stage_group_data["articles"],
                "law_id": last_law_id,
                "init_document_name": init_document_name,
                "parse_time": 0,
                "total_time": 0,
                "article_count": len(stage_group_data["articles"]),
            }
        t0 = time.time()

        articles = stage_group_data["articles"]
        stages_config = stage_group_data["stages"]
        last_law_id = None
        last_document_name = None

        # 1. SETUP: Parse HTML & Build Index (Happens ONCE per Law)
        parser = "html.parser"

        strainer = SoupStrainer(["p", "a"])

        prepared_stages = []

        # Pre-process the stages
        for stage in stages_config:
            # Parse
            soup_before = BeautifulSoup(stage["before"]["content"], parser, parse_only=strainer)
            soup_after = BeautifulSoup(stage["after"]["content"], parser, parse_only=strainer)
            last_law_id = stage["after"]["law_id"]
            last_document_name = stage["after"]["document_name"]

            # Index
            tag_index_after = RetrievalService.build_tag_index(soup_after)

            prepared_stages.append({"soup_before": soup_before, "tag_index_after": tag_index_after})

        parse_time = time.time() - t0

        # 2. EXECUTE: Process Articles (Sequential is fine here because data is ready)
        results = []

        for article_list in articles:
            current_result = article_list  # Start with input e.g. ['77', '78']

            # Pass through all stages (usually 1)
            for prep_stage in prepared_stages:
                # Inner Loop: Process specific numbers
                temp_res = []
                for dieu in current_result:
                    dieu = int(dieu)
                    p_tags = RetrievalService.find_p_tags_until_next_dieu_optimized(prep_stage["soup_before"], dieu)
                    a_tags = RetrievalService.extract_a_tags_from_p_list(p_tags)
                    found = RetrievalService.filter_and_transform_a_tags_indexed(a_tags, prep_stage["tag_index_after"])
                    temp_res.extend(found)

                current_result = list(set(temp_res))

            results.append(current_result)

        total_time = time.time() - t0

        return {
            "results": results,
            "law_id": last_law_id,
            "init_document_name": last_document_name,
            "parse_time": parse_time,
            "total_time": total_time,
            "article_count": len(articles),
        }

    @staticmethod
    def process_stage_group_optimize(all_stages_list: list[dict]) -> Any:
        """
        This function runs in a separate process.
        It handles ONE item from 'all_stages' (one law, multiple articles).
        """
        return [
            RetrievalService.process_stage_group(
                stage_group_data,
                stage_group_data["init_law_id"],
                stage_group_data["init_document_name"],
            )
            for stage_group_data in all_stages_list
        ]

    @staticmethod
    def execute_all_stages_parallel(all_stages_list: Any) -> Any:
        """
        Parallelizes the processing of the 'all_stages' list.
        """

        # We map the results back to the original order if needed,
        # but since inputs are independent, appending is fine.

        print(f" Processing {len(all_stages_list)} Law Groups in Parallel...")

        # ProcessPoolExecutor for heavy text processing
        with ProcessPoolExecutor(max_workers=4) as executor:
            # Submit tasks
            future_to_idx = {
                executor.submit(
                    RetrievalService.process_stage_group,
                    stage_data,
                    stage_data["init_law_id"],
                    stage_data["init_document_name"],
                ): idx
                for idx, stage_data in enumerate(all_stages_list)
            }

            # Collect results
            results_map = [None] * len(all_stages_list)

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    data = future.result()
                    results_map[idx] = {
                        "law_id": data["law_id"],
                        "results": data["results"],
                        "document_name": data["init_document_name"],
                    }

                    print(f" Group {idx} finished: {data['article_count']} articles.")
                    print(f"   - Parse Time: {data['parse_time']:.4f}s")
                    print(f"   - Total Time: {data['total_time']:.4f}s")

                except Exception as exc:
                    print(f"❌ Group {idx} generated an exception: {exc}")
                    import traceback

                    traceback.print_exc()

        return results_map

    @staticmethod
    def execute_all_stages_parallel_optimize(all_stages_list: Any, *args, **kwargs) -> Any:
        """
        Parallelizes the processing of the 'all_stages' list.
        """

        # We map the results back to the original order if needed,
        # but since inputs are independent, appending is fine.

        print(f" Processing {len(all_stages_list)} Law Groups in Parallel...")

        # Collect results
        results_map = [None] * len(all_stages_list)
        state_group_result = RetrievalService.process_stage_group_optimize(all_stages_list)

        for idx, data in enumerate(state_group_result):
            try:
                results_map[idx] = {
                    "law_id": data["law_id"],
                    "results": data["results"],
                    "document_name": data["init_document_name"],
                }

                print(f" Group {idx} finished: {data['article_count']} articles.")
                print(f"   - Parse Time: {data['parse_time']:.4f}s")
                print(f"   - Total Time: {data['total_time']:.4f}s")

            except Exception as exc:
                print(f"❌ Group {idx} generated an exception: {exc}")
                import traceback

                traceback.print_exc()

        return results_map

    # Process each replacement result concurrently
    async def process_replacement(self, ress: Any) -> Any:
        law_id = ress.get("law_id")
        document_name = ress.get("document_name")
        list_ccpl_content = await self.search.get_content_by_articles_and_lawid(ress.get("numbers"), law_id)

        ccpl_content_items = []
        retrieved_content_items = []

        for item in list_ccpl_content:
            # Add to ccpl_content_
            ccpl_content_items.append(
                {
                    "text": item.get("new_text"),
                    "dieu": item.get("dieu"),
                    "law_id": ress.get("law_id"),
                    "document_name": document_name,
                    "News_Effectless": item.get("News_Effectless"),
                    "link": item.get("link"),
                    "News_EffectDate": item.get("News_EffectDate"),
                }
                if item.get("new_text")
                else {
                    "text": item.get("text"),
                    "dieu": item.get("dieu"),
                    "law_id": ress.get("law_id"),
                    "document_name": document_name,
                    "News_Effectless": item.get("News_Effectless"),
                    "link": item.get("link"),
                    "News_EffectDate": item.get("News_EffectDate"),
                }
            )

            if item.get("new_text"):
                retrieved_content_items.append(
                    {
                        "text": item.get("new_text"),
                        "dieu": item.get("dieu"),
                        "law_id": ress.get("law_id"),
                        "document_name": document_name,
                        "News_Effectless": item.get("News_Effectless"),
                        "link": item.get("link"),
                        "News_EffectDate": item.get("News_EffectDate"),
                    }
                )
            else:
                retrieved_content_items.append(
                    {
                        "text": item.get("text"),
                        "dieu": item.get("dieu"),
                        "law_id": ress.get("law_id"),
                        "document_name": document_name,
                        "News_Effectless": item.get("News_Effectless").isoformat()
                        if item.get("News_Effectless")
                        else None,
                        "link": item.get("link"),
                        "News_EffectDate": item.get("News_EffectDate"),
                    }
                )

        return ccpl_content_items, retrieved_content_items

    async def run_retrieve_in_vanban_phase(
        self, questions_list: list[str], top_k: int = 5, score_threshold: float = 0.7
    ) -> Any:
        try:
            start_time = time.time()
            all_results = await self.search.search_law_documents(
                queries=questions_list, limit=top_k, score_threshold=score_threshold
            )
            vanban_cauhoi = []
            uniquee_cauhoi = set()
            for item in all_results.points:
                res = item.payload.get("metadata").get("legal_ground").get("code")
                law_id = str(res).split("_")[0]
                article = str(res).split("_")[1]
                key = f"{law_id}_{article}"
                if key not in uniquee_cauhoi:
                    uniquee_cauhoi.add(key)
                    vanban_cauhoi.append(
                        {
                            "law_id": int(law_id),
                            "article": int(article),
                        }
                    )
            groupedd = defaultdict(list)
            for item in vanban_cauhoi:
                groupedd[item["law_id"]].append(item["article"])

            # 2. Convert back to list of dictionaries
            result = [{"law_id": k, "articles": v} for k, v in groupedd.items()]
            elapsed_time = time.time() - start_time
            print(f"[TIME LOG] run_retrieve_in_vanban_phase: {elapsed_time:.2f}s")
            return result
        except Exception as e:
            raise e

    async def run_retrieval_htpl_phase(
        self, questions: list[str], max_retries: int = 3, top_k: int = 5, score_threshold: float = 0.7
    ) -> Any:
        try:
            start_time = time.time()
            final_retrieval = []
            for question in questions:
                for attempt in range(max_retries + 1):
                    try:
                        data_1 = await self.search.search_documents(
                            queries=[question], limit=top_k, score_threshold=score_threshold
                        )
                        processed_ids_1 = [
                            {
                                "extracted_question": result.payload.get("metadata").get("extracted_question"),
                                "extracted_answer": result.payload.get("metadata").get("extracted_answer"),
                                "rewrite_question": result.payload.get("metadata").get("rewrite_question"),
                                "similarity_score": result.payload.get("metadata").get("similarity_score"),
                                "rewrite_answer": result.payload.get("metadata").get("rewrite_answer"),
                                "field_type": result.payload.get("metadata").get("field_types"),
                                "articles_associations": result.payload.get("metadata").get("legal_ground_codes"),
                            }
                            for result in data_1.points
                        ]

                        final_retrieval.extend(processed_ids_1)
                        break

                    except Exception:
                        if attempt < max_retries:
                            await asyncio.sleep(2**attempt)
                        else:  # Continue with empty results for this question
                            final_retrieval.extend([])

            unique_list = []
            seen = set()
            for item in final_retrieval:
                key = json.dumps(
                    sorted(
                        [
                            {"law_id": str(article).split("_")[0], "article": str(article).split("_")[1]}
                            for article in item["articles_associations"]
                        ],
                        key=lambda x: (x["law_id"], x["article"]),
                    )
                )
                if key not in seen:
                    articles_associations = item["articles_associations"]
                    item["articles_associations"] = [
                        {"id": article, "law_id": str(article).split("_")[0], "article": str(article).split("_")[1]}
                        for article in articles_associations
                    ]
                    unique_list.append(item)
                    seen.add(key)

            if len(unique_list) == 0:
                unique_list = [
                    {
                        "rewrite_question": "Không có dữ liệu",
                        "extracted_question": "N/A",
                        "extracted_answer": "N/A",
                    }
                ]

            # NOTE: Rerank Here by extract question, answer, etc.
            # get unique articles associations
            seen_ids = set()
            unique_articles_associations = []
            for item in unique_list:
                if item.get("articles_associations") is not None:
                    for association in item.get("articles_associations"):
                        if association.get("id") not in seen_ids:
                            seen_ids.add(association.get("id"))
                            if association.get("article") != "-1" and association.get("article") != "0":
                                unique_articles_associations.append(association)

            unique_law_ids = []
            for association in unique_articles_associations:
                unique_law_ids.append(
                    {
                        "law_id": int(association.get("law_id")),
                        "article": int(association.get("article")),
                    }
                )

            grouped = defaultdict(list)
            for item in unique_law_ids:
                grouped[item["law_id"]].append(item["article"])
            result = []
            for law_id in grouped:
                val = [grouped[law_id][0]] if len(grouped[law_id]) == 1 else list(grouped[law_id])

                result.append({"law_id": law_id, "articles": val})
            elapsed_time = time.time() - start_time
            print(f"[TIME LOG] run_retrieval_htpl_phase: {elapsed_time:.2f}s")
            return result
        except Exception as e:
            raise e

    async def run_retrieval_hyde_drill_phase(
        self, questions: list[str], top_k: int = 5, score_threshold: float = 0.7
    ) -> Any:
        try:
            start_time = time.time()
            vanban_cauhoi = []
            uniquee_cauhoi = set()
            datas = await self.search.search_drill_documents(
                queries=questions, top_k=top_k, score_threshold=score_threshold
            )
            for data in datas:
                query_points = data.points
                for point in query_points:
                    code = point.payload.get("metadata").get("code")
                    law_id = code.split("_")[0]
                    article = code.split("_")[1]
                    key = f"{law_id}_{article}"
                    if key not in uniquee_cauhoi:
                        uniquee_cauhoi.add(key)
                        vanban_cauhoi.append(
                            {
                                "law_id": int(law_id),
                                "article": int(article),
                            }
                        )
            grouped = defaultdict(list)
            for item in vanban_cauhoi:
                grouped[item["law_id"]].append(item["article"])
            result = [{"law_id": k, "articles": v} for k, v in grouped.items()]
            elapsed_time = time.time() - start_time
            print(f"[TIME LOG] run_retrieval_hyde_drill_phase: {elapsed_time:.2f}s")
            return result
        except Exception as e:
            raise e

    async def run_retrieval_hyde_phase(self, hyde_answer: str, top_k: int = 5, score_threshold: float = 0.7) -> Any:
        try:
            phase_time = time.time()
            pattern = r"\[.*?\]%%\[.*?\]%%\[.*?\]"
            raw_list = re.findall(pattern, hyde_answer)

            parsed_data = []
            unique_doc_names = set()

            for item in raw_list:
                parts = item.split("%%")
                if len(parts) >= 3:
                    parsed_data.append((parts[1], parts[2]))
                    unique_doc_names.add(parts[2].strip("[]"))

            if not parsed_data:
                print("No valid CCPL data found in first_answer.")
                return []
            phase1_time = time.time() - phase_time
            print(f"⏱️ [1/5] Parsing & Deduplication: {phase1_time:.4f}s | Items: {len(parsed_data)}")

            phase_time = time.time()
            doc_map = {}

            try:
                rows = await self.search.find_legal_documents_by_batch(unique_doc_names)
                for row in rows:
                    doc_map[row["doc_name"].lower()] = int(row["law_id"])
            except Exception as e:
                print(f"Error looking up documents in batch: {e}")
            phase2_time = time.time() - phase_time
            print(f"⏱️ [2/5] Document Lookup: {phase2_time:.4f}s | Items: {len(doc_map)}")

            phase_time = time.time()
            texts_to_embed = []
            metadata_batch = []
            try:
                for content, doc_name in parsed_data:
                    doc_name = doc_name.strip("[]").lower()
                    # Only include doc_name if it occurs after its first position in doc_map
                    if doc_name in doc_map:
                        law_row = doc_map[doc_name]

                        # Text Processing
                        processed_text = str(preprocess(f"{doc_name} {content.strip('[]')}"))

                        texts_to_embed.append(processed_text)
                        metadata_batch.append(
                            {"law_id": law_row, "doc_name": doc_name, "processed_text": processed_text}
                        )

                if not texts_to_embed:
                    print("No valid CCPL data found in first_answer.")
                    return []

                phase3_time = time.time() - phase_time
                print(f"⏱️ [3/5] Text Preprocessing: {phase3_time:.4f}s | Batch Size: {len(texts_to_embed)}")
            except Exception as e:
                print(f"Error preprocessing text: {e}")
                return []

            phase_time = time.time()
            all_list = []
            final_result = []
            try:
                results = await self.search.find_ccpl_by_first_answer(metadata_batch)
                for result in results:
                    points = result.points
                    for point in points:
                        payload = point.payload.get("metadata")
                        law_id = payload.get("law_id")
                        bm_code = payload.get("bm_code")
                        article = bm_code.split("_")[1]
                        all_list.append({"law_id": law_id, "article": article})

                grouped = defaultdict(list)
                for item in all_list:
                    grouped[item["law_id"]].append(item["article"])
                final_result = [{"law_id": k, "articles": v} for k, v in grouped.items()]

            except Exception as e:
                print(f"Error finding CCPL by first answer: {e}")
                return []

            return final_result
        except Exception as e:
            raise e

    async def postprocess_pipeline(
        self,
        normal_queries: list[str],
        hyde_drill_queries: list[str],
        hyde_answer: str,
        top_k: int,
        score_threshold: float,
        query_timestamp: int = int(time.time()),
    ) -> list[dict]:
        retrieve_vanban_task = asyncio.create_task(
            self.run_retrieve_in_vanban_phase(
                questions_list=normal_queries, top_k=top_k, score_threshold=score_threshold
            )
        )

        retrieval_htpl_task = asyncio.create_task(
            self.run_retrieval_htpl_phase(questions=normal_queries, top_k=top_k, score_threshold=score_threshold)
        )

        retrieve_hyde_drill = asyncio.create_task(
            self.run_retrieval_hyde_drill_phase(
                questions=hyde_drill_queries, top_k=top_k, score_threshold=score_threshold
            )
        )

        retrieve_hyde = asyncio.create_task(
            self.run_retrieval_hyde_phase(hyde_answer=hyde_answer, top_k=top_k, score_threshold=score_threshold)
        )

        (
            retrieve_vanban_results,
            retrieval_htpl_results,
            retrieve_hyde_drill_results,
            retrieve_hyde_results,
        ) = await asyncio.gather(retrieve_vanban_task, retrieval_htpl_task, retrieve_hyde_drill, retrieve_hyde)

        full_ccpls = (
            retrieval_htpl_results + retrieve_vanban_results + retrieve_hyde_drill_results + retrieve_hyde_results
        )

        merged_dict = {}

        for item in full_ccpls:
            lid = item["law_id"]
            articles = item["articles"]
            if lid not in merged_dict:
                merged_dict[lid] = []
            # Extend the current list with the new articles
            merged_dict[lid].extend(articles)

        # 3. Format back to list and remove duplicates
        final_result = []
        for lid, all_articles in merged_dict.items():
            # dict.fromkeys() is a fast trick to remove duplicates while preserving Order
            # (Sets would scramble the order)
            unique_articles = list(dict.fromkeys(all_articles))
            final_result.append({"law_id": lid, "articles": unique_articles})
        distinct_ids = list({item["law_id"] for item in final_result if "law_id" in item})

        document_names = await self.search.get_document_name_by_many_ids(distinct_ids)
        document_names_dict = {item["LawID"]: item["News_Subject"] for item in document_names}
        for item in final_result:
            item["init_document_name"] = document_names_dict.get(item["law_id"], "")

        final_results = final_result

        # tasks = [
        #     self.search.get_stages_by_lawid(result["law_id"], result["articles"], result["init_document_name"])
        #     for result in final_results
        # ]

        # results = await asyncio.gather(*tasks, return_exceptions=True)

        results = await self.search.get_stages_by_lawid_all(final_results)

        all_stages = []

        for _, stages in zip(final_results, results, strict=False):
            if isinstance(stages, Exception):
                continue

            if stages != {}:
                all_stages.append(stages)

        # final_results_after_stages = self.execute_all_stages_parallel(all_stages)
        final_results_after_stages = self.execute_all_stages_parallel_optimize(all_stages)
        merged_data = defaultdict(lambda: {"numbers": set(), "document_name": None})

        # 2. Process and Merge the data
        for item in final_results_after_stages:
            law_id = item["law_id"]

            # Store document name (overwrites if duplicates exist)
            merged_data[law_id]["document_name"] = item["document_name"]

            # Merge numbers using set .update() (handles uniqueness automatically)
            for sublist in item["results"]:
                merged_data[law_id]["numbers"].update([int(i) for i in sublist])

        # 3. Format the final output as a LIST of dictionaries
        replacement_results = [
            {
                "law_id": lid,
                "numbers": sorted(data["numbers"]),
                "document_name": data["document_name"],
            }
            for lid, data in merged_data.items()
        ]

        tasks = [self.process_replacement(ress) for ress in replacement_results]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Combine all results
        list_retrieved_content = []
        list_ccpl_content_ = []
        for ccpl_items, retrieved_items in results:
            list_ccpl_content_.extend(ccpl_items)
            list_retrieved_content.extend(retrieved_items)

        unique_retrieved_content = set()
        unique_list_retrieved_content = []

        for item in list_retrieved_content:
            key = f"{item.get('law_id')}_{item.get('dieu')}"
            if key not in unique_retrieved_content:
                unique_retrieved_content.add(key)
                unique_list_retrieved_content.append(item)

        # Filter out documents that are not in effect on the query date
        unique_retrieved_content = [
            law
            for law in unique_list_retrieved_content
            if (
                law["News_EffectDate"] is not None
                and law["News_EffectDate"].timestamp() <= query_timestamp
                and (law["News_Effectless"] is None or law["News_EffectDate"].timestamp() > query_timestamp)
            )
        ]

        used_dieu = []
        for doc in unique_retrieved_content:
            used_dieu.append({"dieu": doc.get("dieu"), "law_id": doc.get("law_id")})

        grouped = {}
        for item in used_dieu:
            lid = item["law_id"]
            if lid not in grouped:
                grouped[lid] = []
            grouped[lid].append(item["dieu"])

        # 2. Transform into the desired list format
        grouped_result = [{"law_id": k, "numbers": v} for k, v in grouped.items()]

        ccpl_relation_list = []

        tasks = [self.search.process_single_rep_result_first(rep) for rep in grouped_result]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            ccpl_relation_list.extend(result)

        return unique_retrieved_content, ccpl_relation_list

    async def retrieval(
        self,
        normal_queries: list[str],
        hyde_drill_queries: list[str],
        hyde_answer: str,
        top_k: int = 5,
        score_threshold: float = 0.7,
        query_timestamp: int = int(time.time()),
    ) -> dict:
        normal_queries = self.preprocess_pipeline(normal_queries)
        hyde_drill_queries = self.preprocess_pipeline(hyde_drill_queries)
        replace_articles, change_articles = await self.postprocess_pipeline(
            normal_queries=normal_queries,
            hyde_drill_queries=hyde_drill_queries,
            hyde_answer=hyde_answer,
            top_k=top_k,
            score_threshold=score_threshold,
            query_timestamp=query_timestamp,
        )
        result = {"replace_articles": replace_articles, "change_articles": change_articles}

        # result = await self.run_retrieval_hyde_phase(
        #     hyde_answer=hyde_answer, top_k=top_k, score_threshold=score_threshold
        # )
        return result

    async def postprocess_contract(
        self,
        normal_queries: list[str],
        top_k: int = 5,
        score_threshold: float = 0.7,
    ) -> dict:
        contract_ids = await self.search.search_contract_ids(
            queries=normal_queries, limit=top_k, score_threshold=score_threshold
        )

        contracts = await self.search.search_contracts(contract_ids)
        contracts = [dict(contract) for contract in contracts]
        for contract in contracts:
            contract["create_day"] = contract["create_day"].strftime("%Y-%m-%d")
        return contracts

    async def retrieval_contract(
        self,
        normal_queries: list[str],
        top_k: int = 5,
        score_threshold: float = 0.7,
    ) -> dict:
        normal_queries = self.preprocess_pipeline(normal_queries)
        contracts = await self.postprocess_contract(
            normal_queries=normal_queries,
            top_k=top_k,
            score_threshold=score_threshold,
        )

        return {"contracts": contracts}

    async def postprocess_file(
        self,
        normal_queries: list[str],
        top_k: int = 5,
        score_threshold: float = 0.7,
        file_ids: list[str] | None = None,
        source: Literal["UPLOAD", "EDITOR", "ALL"] = "ALL",
    ) -> dict:
        files = await self.search.search_file(
            queries=normal_queries,
            limit=top_k,
            score_threshold=score_threshold,
            file_ids=file_ids,
            source=source,
        )

        file_ids = list({f["id"].split("_")[0] for f in files})

        full_file_contents = await self.search.search_full_files(file_ids)

        return {
            str(f["id"]): {
                "full_content": f["structured_data"],
                "chunk": [mf for mf in files if str(mf["id"]).startswith(str(f["id"]))],
            }
            for f in full_file_contents
        }

    async def retrieval_file(
        self,
        normal_queries: list[str],
        top_k: int = 5,
        score_threshold: float = 0.7,
        file_ids: list[str] | None = None,
        source: Literal["UPLOAD", "EDITOR", "ALL"] = "ALL",
    ) -> dict:
        normal_queries = self.preprocess_pipeline(normal_queries)
        files = await self.postprocess_file(
            normal_queries=normal_queries,
            top_k=top_k,
            score_threshold=score_threshold,
            file_ids=file_ids,
            source=source,
        )

        return {"files": files}
