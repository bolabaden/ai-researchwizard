from __future__ import annotations

import asyncio
import logging
import random

from typing import TYPE_CHECKING, Any, Callable

from gpt_researcher.actions.agent_creator import choose_agent
from gpt_researcher.actions.query_processing import get_search_results, plan_research_outline
from gpt_researcher.actions.utils import stream_output
from gpt_researcher.document import DocumentLoader, LangChainDocumentLoader, OnlineDocumentLoader
from gpt_researcher.retrievers.retriever_abc import RetrieverABC
from gpt_researcher.utils.enum import ReportSource
from gpt_researcher.utils.logging_config import get_json_handler

if TYPE_CHECKING:
    from gpt_researcher.agent import GPTResearcher


class ResearchConductor:
    """Manages and coordinates the research process."""

    def __init__(self, researcher: GPTResearcher):
        self.researcher: GPTResearcher = researcher
        self.logger: logging.Logger = logging.getLogger("research")
        self.json_handler: Any | None = get_json_handler()
        # Add cache for MCP results to avoid redundant calls
        self._mcp_results_cache = None
        # Track MCP query count for balanced mode
        self._mcp_query_count = 0

    async def plan_research(
        self,
        query: str,
        query_domains: list[str] | None = None,
    ) -> list[str]:
        """Gets the sub-queries from the query.

        Args:
            query: original query

        Returns:
            List of queries
        """
        self.logger.info(f"Planning research for query: '''{query}'''")

        await stream_output(
            "logs",
            "planning_research",
            f"🌐 Browsing the web to learn more about the task: '''{query}'''...",
            self.researcher.websocket,
        )

        search_results: list[dict[str, Any]] = await get_search_results(
            query,
            self.researcher.retrievers[0],
            query_domains,
            researcher=self.researcher,
        )
        self.logger.info(f"Initial search results obtained: {len(search_results)} results")

        await stream_output(
            "logs",
            "planning_research",
            "🤔 Planning the research strategy and subtasks...",
            self.researcher.websocket,
        )

        outline: list[str] = await plan_research_outline(
            query=query,
            search_results=search_results,
            agent_role_prompt=self.researcher.role,
            cfg=self.researcher.cfg,
            parent_query=self.researcher.parent_query,
            report_type=self.researcher.report_type,
            cost_callback=self.researcher.add_costs,
            retriever_names=[r.__name__ for r in self.researcher.retrievers],  # Pass retriever names for MCP optimization
            **self.researcher.kwargs
        )
        self.logger.info(f"Research outline planned: {outline}")
        return outline

    async def conduct_research(self) -> list[str]:
        """Runs the GPT Researcher to conduct research"""
        if self.json_handler is not None:
            self.json_handler.update_content("query", self.researcher.query)

        self.logger.info(f"Starting research for query: {self.researcher.query}")

        # Log active retrievers once at the start of research
        retriever_names = [r.__name__ for r in self.researcher.retrievers]
        self.logger.info(f"Active retrievers: {retriever_names}")

        # Reset visited_urls and source_urls at the start of each research task
        self.researcher.visited_urls = set() if self.researcher.visited_urls is None else self.researcher.visited_urls
        research_data: list[str] = []

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "starting_research",
                f"🔍 Starting the research task for '{self.researcher.query}'...",
                self.researcher.websocket,
            )

        if self.researcher.verbose:
            await stream_output("logs", "agent_generated", self.researcher.agent, self.researcher.websocket)

        # Choose agent and role if not already defined
        if not (self.researcher.agent and self.researcher.role):
            self.researcher.agent, self.researcher.role = await choose_agent(
                query=self.researcher.query,
                cfg=self.researcher.cfg,
                parent_query=self.researcher.parent_query,
                cost_callback=self.researcher.add_costs,
                headers=self.researcher.headers,
                prompt_family=self.researcher.prompt_family
            )

        # Check if MCP retrievers are configured
        has_mcp_retriever = any("mcpretriever" in r.__name__.lower() for r in self.researcher.retrievers)
        if has_mcp_retriever:
            self.logger.info("MCP retrievers configured and will be used with standard research flow")

        # Conduct research based on the source type
        if self.researcher.source_urls:
            self.logger.info("Using provided source URLs")
            research_data = await self._get_context_by_urls(self.researcher.source_urls)
            if research_data and len(research_data) == 0 and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "answering_from_memory",
                    "🧐 I was unable to find relevant context in the provided sources...",
                    self.researcher.websocket,
                )
            if self.researcher.complement_source_urls:
                self.logger.info("Complementing with web search")
                additional_research: list[str] = await self._get_context_by_web_search(self.researcher.query)
                research_data += " ".join(additional_research)

        elif self.researcher.report_source == ReportSource.Web.value:
            self.logger.info("Using web search")
            research_data = await self._get_context_by_web_search(self.researcher.query)

        elif self.researcher.report_source == ReportSource.Local.value:
            self.logger.info("Using local search")
            document_data: list[str] = await DocumentLoader(self.researcher.cfg.doc_path).load()
            self.logger.info(f"Loaded {len(document_data)} documents")
            if self.researcher.vector_store:
                self.researcher.vector_store.load(document_data)

            research_data = await self._get_context_by_web_search(self.researcher.query, document_data, self.researcher.query_domains)

        # Hybrid search including both local documents and web sources
        elif self.researcher.report_source == ReportSource.Hybrid.value:
            document_data: list[str] = (
                DocumentLoader(self.researcher.cfg.doc_path)
                if self.researcher.document_urls is None
                else OnlineDocumentLoader(self.researcher.document_urls)
            ).load()
            if self.researcher.vector_store is not None:
                self.researcher.vector_store.load(document_data)
            docs_context: list[str] = await self._get_context_by_web_search(self.researcher.query, document_data)
            web_context: list[str] = await self._get_context_by_web_search(self.researcher.query)
            research_data = f"Context from local documents: {docs_context}\n\nContext from web sources: {web_context}"
            docs_context: list[str] = await self._get_context_by_web_search(
                self.researcher.query,
                document_data,
                self.researcher.query_domains,
            )
            web_context: list[str] = await self._get_context_by_web_search(
                self.researcher.query,
                [],
                self.researcher.query_domains,
            )
            research_data = self.researcher.prompt_family.join_local_web_documents(docs_context, web_context)

        elif self.researcher.report_source == ReportSource.LangChainDocuments.value:
            langchain_documents_data: list[dict[str, Any]] = await LangChainDocumentLoader(self.researcher.documents).load()
            if self.researcher.vector_store is not None:
                self.researcher.vector_store.load(langchain_documents_data)
            research_data = await self._get_context_by_web_search(
                self.researcher.query,
                langchain_documents_data,
            )
        elif self.researcher.report_source == ReportSource.LangChainVectorStore.value:
            research_data = await self._get_context_by_vectorstore(
                self.researcher.query,
                self.researcher.vector_store_filter,
            )

        # Rank and curate the sources
        self.researcher.context = research_data
        if self.researcher.cfg.curate_sources:  # pyright: ignore[reportAttributeAccessIssue]
            self.logger.info("Curating sources")
            self.logger.info(f"Data passed to curate_sources (research_data type: {type(research_data)}, length: {len(research_data) if isinstance(research_data, list) else 'N/A'}): {research_data}")
            curated_sources: list[dict[str, Any]] = await self.researcher.source_curator.curate_sources(research_data)
            self.logger.info(f"Data returned from curate_sources (curated_sources type: {type(curated_sources)}, length: {len(curated_sources)}): {curated_sources}")

            # Extract content from curated sources to maintain list[str] format for context
            # The curated sources are dictionaries with 'raw_content' key, but context should be list[str]
            if curated_sources and isinstance(curated_sources[0], dict):
                # Process curated sources through context manager to get string content
                curated_content: str = await self.researcher.context_manager.get_similar_content_by_query(
                    self.researcher.query,
                    curated_sources,
                )
                self.researcher.context = [curated_content] if curated_content else research_data
            else:
                # Fallback to original research_data if curation didn't return expected format
                self.researcher.context = research_data

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "research_step_finalized",
                f"Finalized research step.\n💸 Total Research Costs: ${self.researcher.get_costs()}",
                self.researcher.websocket,
            )
            if self.json_handler is not None:
                self.json_handler.update_content("costs", self.researcher.get_costs())
                self.json_handler.update_content("context", self.researcher.context)

        self.logger.info(f"Research completed. Context size: {len(str(self.researcher.context))}")
        return self.researcher.context

    async def _get_context_by_urls(
        self,
        urls: list[str],
    ) -> list[str]:
        """Scrapes and compresses the context from the given urls"""
        self.logger.info(f"Getting context from URLs: {urls}")

        new_search_urls: list[str] = await self._get_new_urls(urls)
        self.logger.info(f"New URLs to process: {new_search_urls}")

        scraped_content: list[dict[str, Any]] = await self.researcher.scraper_manager.browse_urls(new_search_urls)
        self.logger.info(f"Scraped content from {len(scraped_content)} URLs")

        if self.researcher.vector_store:
            self.researcher.vector_store.load(scraped_content)

        context: str = await self.researcher.context_manager.get_similar_content_by_query(
            self.researcher.query,
            scraped_content,
        )
        self.logger.info(f"Generated context length: {len(context)}")
        return [context] if context else []

    # Add logging to other methods similarly...

    async def _get_context_by_vectorstore(
        self,
        query: str,
        vectorstore_filter: dict[str, Any] | None = None,
    ) -> list[str]:
        """Generates the context for the research task by searching the vectorstore

        Args:
            query (str): The query to search the vectorstore for
            filter (dict[str, Any] | None): The filter to apply to the vectorstore

        Returns:
            context: List of context
        """
        context: list[str] = []
        # Generate Sub-Queries including original query
        sub_queries: list[str] = await self.plan_research(query)
        # If this is not part of a sub researcher, add original query to research for better results
        if self.researcher.report_type != "subtopic_report":
            sub_queries.append(query)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "subqueries",
                f"🗂️  I will conduct my research based on the following queries: {sub_queries}...",
                self.researcher.websocket,
                True,
                sub_queries,
            )

        # Using asyncio.gather to process the sub_queries asynchronously
        context = await asyncio.gather(*[self._process_sub_query_with_vectorstore(sub_query, vectorstore_filter) for sub_query in sub_queries])
        return context

    def _get_mcp_strategy(self) -> str:
        """
        Get the MCP strategy configuration.

        Priority:
        1. Instance-level setting (self.researcher.mcp_strategy)
        2. Config file setting (self.researcher.cfg.mcp_strategy)
        3. Default value ("fast")

        Returns:
            str: MCP strategy
                "disabled" = Skip MCP entirely
                "fast" = Run MCP once with original query (default)
                "deep" = Run MCP for all sub-queries
        """
        # Check instance-level setting first
        if hasattr(self.researcher, 'mcp_strategy') and self.researcher.mcp_strategy is not None:
            return self.researcher.mcp_strategy

        # Check config setting
        if hasattr(self.researcher.cfg, 'mcp_strategy'):
            return self.researcher.cfg.mcp_strategy

        # Default to fast mode
        return "fast"

    async def _execute_mcp_research_for_queries(self, queries: list, mcp_retrievers: list) -> list:
        """
        Execute MCP research for a list of queries.

        Args:
            queries: List of queries to research
            mcp_retrievers: List of MCP retriever classes

        Returns:
            list: Combined MCP context entries from all queries
        """
        all_mcp_context = []

        for i, query in enumerate(queries, 1):
            self.logger.info(f"Executing MCP research for query {i}/{len(queries)}: {query}")

            for retriever in mcp_retrievers:
                try:
                    mcp_results = await self._execute_mcp_research(retriever, query)
                    if mcp_results:
                        for result in mcp_results:
                            content = result.get("body", "")
                            url = result.get("href", "")
                            title = result.get("title", "")

                            if content:
                                context_entry = {
                                    "content": content,
                                    "url": url,
                                    "title": title,
                                    "query": query,
                                    "source_type": "mcp"
                                }
                                all_mcp_context.append(context_entry)

                        self.logger.info(f"Added {len(mcp_results)} MCP results for query: {query}")

                        if self.researcher.verbose:
                            await stream_output(
                                "logs",
                                "mcp_results_cached",
                                f"✅ Cached {len(mcp_results)} MCP results from query {i}/{len(queries)}",
                                self.researcher.websocket,
                            )
                except Exception as e:
                    self.logger.error(f"Error in MCP research for query '{query}': {e}")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_cache_error",
                            f"⚠️ MCP research error for query {i}, continuing with other sources",
                            self.researcher.websocket,
                        )

        return all_mcp_context

    async def _get_context_by_web_search(
        self,
        query: str,
        scraped_data: list[str] | None = None,
        query_domains: list[str] | None = None,
    ) -> list[str]:
        """Generates the context for the research task by searching the query and scraping the results.
        Args:
            query (str): The query to search the web for
            scraped_data (list[str] | None): The scraped data to use for the research
            query_domains (list[str] | None): The domains to search the web for

        Returns:
            context: List of context
        """
        self.logger.info(f"Starting web search for query: '''{query}'''")

        # **CONFIGURABLE MCP OPTIMIZATION: Control MCP strategy**
        mcp_retrievers = [r for r in self.researcher.retrievers if "mcpretriever" in r.__name__.lower()]

        # Get MCP strategy configuration
        mcp_strategy: str = self._get_mcp_strategy()

        if mcp_retrievers and self._mcp_results_cache is None:
            if mcp_strategy == "disabled":
                # MCP disabled - skip MCP research entirely
                self.logger.info("MCP disabled by strategy, skipping MCP research")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_disabled",
                        f"⚡ MCP research disabled by configuration",
                        self.researcher.websocket,
                    )
            elif mcp_strategy == "fast":
                # Fast: Run MCP once with original query
                self.logger.info("MCP fast strategy: Running once with original query")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_optimization",
                        f"🚀 MCP Fast: Running once for main query (performance mode)",
                        self.researcher.websocket,
                    )

                # Execute MCP research once with the original query
                mcp_context = await self._execute_mcp_research_for_queries([query], mcp_retrievers)
                self._mcp_results_cache = mcp_context
                self.logger.info(f"MCP results cached: {len(mcp_context)} total context entries")
            elif mcp_strategy == "deep":
                # Deep: Will run MCP for all queries (original behavior) - defer to per-query execution
                self.logger.info("MCP deep strategy: Will run for all queries")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_comprehensive",
                        f"🔍 MCP Deep: Will run for each sub-query (thorough mode)",
                        self.researcher.websocket,
                    )
                # Don't cache - let each sub-query run MCP individually
            else:
                # Unknown strategy - default to fast
                self.logger.warning(f"Unknown MCP strategy '{mcp_strategy}', defaulting to fast")
                mcp_context = await self._execute_mcp_research_for_queries([query], mcp_retrievers)
                self._mcp_results_cache = mcp_context
                self.logger.info(f"MCP results cached: {len(mcp_context)} total context entries")

        # Generate Sub-Queries including original query
        sub_queries: list[str] = await self.plan_research(query)
        self.logger.info(f"Generated sub-queries: {sub_queries}")

        # If this is not part of a sub researcher, add original query to research for better results
        if self.researcher.report_type != "subtopic_report":
            sub_queries.append(query)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "subqueries",
                f"🗂️ I will conduct my research based on the following queries: {sub_queries}...",
                self.researcher.websocket,
                True,
                sub_queries,
            )

        # Using asyncio.gather to process the sub_queries asynchronously
        try:
            context: list[str] = await asyncio.gather(*[self._process_sub_query(sub_query, scraped_data or []) for sub_query in sub_queries])
            self.logger.info(f"Gathered context from {len(context)} sub-queries")
            # Filter out empty results and join the context
            context = [c for c in context if c]
            if context:
                return context
            return []
        except Exception as e:
            self.logger.error(f"Error during web search: {e.__class__.__name__}: {e}", exc_info=True)
            return []

    async def _process_sub_query(
        self,
        sub_query: str,
        scraped_data: list[str] | None = None,
        query_domains: list[str] | None = None,
    ) -> str:
        """Takes in a sub query and scrapes urls based on it and gathers context."""
        if self.json_handler is not None:
            self.json_handler.log_event("sub_query", {"query": sub_query, "scraped_data_size": len(scraped_data)})

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "running_subquery_research",
                f"\n🔍 Running research for '{sub_query}'...",
                self.researcher.websocket,
            )

        try:
            # Identify MCP retrievers
            mcp_retrievers = [r for r in self.researcher.retrievers if "mcpretriever" in r.__name__.lower()]
            non_mcp_retrievers = [r for r in self.researcher.retrievers if "mcpretriever" not in r.__name__.lower()]

            # Initialize context components
            mcp_context = []
            web_context = ""

            # Get MCP strategy configuration
            mcp_strategy = self._get_mcp_strategy()

            # **CONFIGURABLE MCP PROCESSING**
            if mcp_retrievers:
                if mcp_strategy == "disabled":
                    # MCP disabled - skip entirely
                    self.logger.info(f"MCP disabled for sub-query: {sub_query}")
                elif mcp_strategy == "fast" and self._mcp_results_cache is not None:
                    # Fast: Use cached results
                    mcp_context = self._mcp_results_cache.copy()

                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_cache_reuse",
                            f"♻️ Reusing cached MCP results ({len(mcp_context)} sources) for: {sub_query}",
                            self.researcher.websocket,
                        )

                    self.logger.info(f"Reused {len(mcp_context)} cached MCP results for sub-query: {sub_query}")
                elif mcp_strategy == "deep":
                    # Deep: Run MCP for every sub-query
                    self.logger.info(f"Running deep MCP research for: {sub_query}")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_comprehensive_run",
                            f"🔍 Running deep MCP research for: {sub_query}",
                            self.researcher.websocket,
                        )

                    mcp_context = await self._execute_mcp_research_for_queries([sub_query], mcp_retrievers)
                else:
                    # Fallback: if no cache and not deep mode, run MCP for this query
                    self.logger.warning("MCP cache not available, falling back to per-sub-query execution")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_fallback",
                            f"🔌 MCP cache unavailable, running MCP research for: {sub_query}",
                            self.researcher.websocket,
                        )

                    mcp_context = await self._execute_mcp_research_for_queries([sub_query], mcp_retrievers)

            # Get web search context using non-MCP retrievers (if no scraped data provided)
            if not scraped_data:
                scraped_data = await self._scrape_data_by_urls(sub_query)
                self.logger.info(f"Scraped data size: {len(scraped_data)}")

            # Get similar content based on scraped data
            if scraped_data:
                web_context = await self.researcher.context_manager.get_similar_content_by_query(sub_query, scraped_data)
                self.logger.info(f"Web content found for sub-query: {len(str(web_context)) if web_context else 0} chars")

            # Combine MCP context with web context intelligently
            combined_context = self._combine_mcp_and_web_context(mcp_context, web_context, sub_query)

            # Log context combination results
            if combined_context and self.researcher.verbose:
                context_length = len(str(combined_context))
                self.logger.info(f"Combined context for '{sub_query}': {context_length} chars")

                if self.researcher.verbose:
                    mcp_count = len(mcp_context)
                    web_available = bool(web_context)
                    cache_used = self._mcp_results_cache is not None and mcp_retrievers and mcp_strategy != "deep"
                    cache_status = " (cached)" if cache_used else ""
                    await stream_output(
                        "logs",
                        "context_combined",
                        f"📚 Combined research context: {mcp_count} MCP sources{cache_status}, {'web content' if web_available else 'no web content'}",
                        self.researcher.websocket,
                    )
            else:
                self.logger.warning(f"No combined context found for sub-query: {sub_query}")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "subquery_context_not_found",
                        f"🤷 No content found for '{sub_query}'...",
                        self.researcher.websocket,
                    )

            if combined_context and self.researcher.verbose and self.json_handler:
                self.json_handler.log_event("content_found", {
                    "sub_query": sub_query,
                    "content_size": len(str(combined_context)),
                    "mcp_sources": len(mcp_context),
                    "web_content": bool(web_context)
                })
            if combined_context:
                if self.json_handler:
                    self.json_handler.log_event("content_found", {"sub_query": sub_query, "content_size": len(combined_context)})

            return combined_context

        except Exception as e:
            self.logger.error(f"Error processing sub-query {sub_query}: {e.__class__.__name__}: {e}", exc_info=True)
            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "subquery_error",
                    f"❌ Error processing '{sub_query}': {e.__class__.__name__}: {e}",
                    self.researcher.websocket,
                )
            return ""

    async def _execute_mcp_research(self, retriever, query):
        """
        Execute MCP research using the new two-stage approach.

        Args:
            retriever: The MCP retriever class
            query: The search query

        Returns:
            list: MCP research results
        """
        retriever_name = retriever.__name__

        self.logger.info(f"Executing MCP research with {retriever_name} for query: {query}")

        try:
            # Instantiate the MCP retriever with proper parameters
            # Pass the researcher instance (self.researcher) which contains both cfg and mcp_configs
            retriever_instance = retriever(
                query=query,
                headers=self.researcher.headers,
                query_domains=self.researcher.query_domains,
                websocket=self.researcher.websocket,
                researcher=self.researcher  # Pass the entire researcher instance
            )

            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_retrieval_stage1",
                    f"🧠 Stage 1: Selecting optimal MCP tools for: {query}",
                    self.researcher.websocket,
                )

            # Execute the two-stage MCP search
            results = retriever_instance.search(
                max_results=self.researcher.cfg.max_search_results_per_query
            )

            if results:
                result_count = len(results)
                self.logger.info(f"MCP research completed: {result_count} results from {retriever_name}")

                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_research_complete",
                        f"🎯 MCP research completed: {result_count} intelligent results obtained",
                        self.researcher.websocket,
                    )

                return results
            else:
                self.logger.info(f"No results returned from MCP research with {retriever_name}")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_no_results",
                        f"ℹ️ No relevant information found via MCP for: {query}",
                        self.researcher.websocket,
                    )
                return []

        except Exception as e:
            self.logger.error(f"Error in MCP research with {retriever_name}: {str(e)}")
            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_research_error",
                    f"⚠️ MCP research error: {str(e)} - continuing with other sources",
                    self.researcher.websocket,
                )
            return []

    def _combine_mcp_and_web_context(self, mcp_context: list, web_context: str, sub_query: str) -> str:
        """
        Intelligently combine MCP and web research context.

        Args:
            mcp_context: List of MCP context entries
            web_context: Web research context string
            sub_query: The sub-query being processed

        Returns:
            str: Combined context string
        """
        combined_parts = []

        # Add web context first if available
        if web_context and web_context.strip():
            combined_parts.append(web_context.strip())
            self.logger.debug(f"Added web context: {len(web_context)} chars")

        # Add MCP context with proper formatting
        if mcp_context:
            mcp_formatted = []

            for i, item in enumerate(mcp_context):
                content = item.get("content", "")
                url = item.get("url", "")
                title = item.get("title", f"MCP Result {i+1}")

                if content and content.strip():
                    # Create a well-formatted context entry
                    if url and url != f"mcp://llm_analysis":
                        citation = f"\n\n*Source: {title} ({url})*"
                    else:
                        citation = f"\n\n*Source: {title}*"

                    formatted_content = f"{content.strip()}{citation}"
                    mcp_formatted.append(formatted_content)

            if mcp_formatted:
                # Join MCP results with clear separation
                mcp_section = "\n\n---\n\n".join(mcp_formatted)
                combined_parts.append(mcp_section)
                self.logger.debug(f"Added {len(mcp_context)} MCP context entries")

        # Combine all parts
        if combined_parts:
            final_context = "\n\n".join(combined_parts)
            self.logger.info(f"Combined context for '{sub_query}': {len(final_context)} total chars")
            return final_context
        else:
            self.logger.warning(f"No context to combine for sub-query: {sub_query}")
            return ""

    async def _process_sub_query_with_vectorstore(
        self,
        sub_query: str,
        filter: dict[str, Any] | None = None,
    ) -> str:
        """Takes in a sub query and gathers context from the user provided vector store

        Args:
            sub_query (str): The sub-query generated from the original query
            filter (dict[str, Any] | None): The filter to apply to the vector store

        Returns:
            str: The context gathered from search
        """
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "running_subquery_with_vectorstore_research",
                f"\n🔍 Running research for '{sub_query}'...",
                self.researcher.websocket,
            )

        content: str = await self.researcher.context_manager.get_similar_content_by_query_with_vectorstore(
            sub_query,
            filter,
        )

        if content and self.researcher.verbose:
            await stream_output(
                "logs",
                "subquery_context_window",
                f"📃 {content}",
                self.researcher.websocket,
            )
        elif self.researcher.verbose:
            await stream_output(
                "logs",
                "subquery_context_not_found",
                f"🤷 No content found for '{sub_query}'...",
                self.researcher.websocket,
            )
        return content

    async def _get_new_urls(
        self,
        url_set_input: set[str] | None = None,
    ) -> list[str]:
        """Gets the new urls from the given url set.
        Args:
            url_set_input (set[str] | None): The url set to get the new urls from

        Returns:
            list[str]: The new urls from the given url set
        """

        new_urls: list[str] = []
        for url in url_set_input or []:
            if url not in self.researcher.visited_urls:
                self.researcher.visited_urls.add(url)
                new_urls.append(url)
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "added_source_url",
                        f"✅ Added source url to research: {url}\n",
                        self.researcher.websocket,
                        True,
                        url,
                    )

        return new_urls

    async def _search_relevant_source_urls(self, query: str) -> list[str]:
        """Searches the relevant source urls for the given query.
        Args:
            query (str): The query to search the source urls for
        Returns:
            list[str]: The relevant source urls for the given query
        """

        new_search_urls: list[str] = []
        # Use the new fallback mechanism instead of iterating manually
        if self.researcher.retrievers:
            try:
                # Get first retriever as primary, rest as fallbacks
                primary_retriever: Callable[[str, dict[str, Any] | None], RetrieverABC] | type[RetrieverABC] = self.researcher.retrievers[0]
                fallback_retrievers: list[Callable[[str, dict[str, Any] | None], RetrieverABC] | type[RetrieverABC]] | None = (
                    self.researcher.retrievers[1:] if len(self.researcher.retrievers) > 1 else None
                )

                # Use get_search_results with fallback support
                search_results: list[dict[str, Any]] = await get_search_results(
                    query,
                    primary_retriever,
                    fallback_retrievers=fallback_retrievers,
                    min_results=1,
                )

                # Collect URLs from search results
                search_urls: list[str] = [
                    url.get("href")
                    for url in search_results
                    if url.get("href")
                ]
                new_search_urls.extend(search_urls)
            except Exception as e:
                self.logger.error(f"Error searching with retrievers: {e.__class__.__name__}: {e}")

        # Get unique URLs
        new_search_urls: list[str] = await self._get_new_urls(new_search_urls)
        random.shuffle(new_search_urls)

        return new_search_urls

    async def _scrape_data_by_urls(self, sub_query: str) -> list[str]:
        """Runs a sub-query across multiple retrievers and scrapes the resulting URLs.

        Args:
            sub_query (str): The sub-query to search for.

        Returns:
            list: A list of scraped content results.
        """
        new_search_urls: list[str] = await self._search_relevant_source_urls(sub_query)

        # Log the research process if verbose mode is on
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "researching",
                "🤔 Researching for relevant information across multiple sources...\n",
                self.researcher.websocket,
            )

        # Scrape the new URLs
        scraped_content: list[str] = await self.researcher.scraper_manager.browse_urls(new_search_urls)

        if self.researcher.vector_store is not None:
            self.researcher.vector_store.load(scraped_content)

        return scraped_content

    async def _search(self, retriever, query):
        """
        Perform a search using the specified retriever.

        Args:
            retriever: The retriever class to use
            query: The search query

        Returns:
            list: Search results
        """
        retriever_name = retriever.__name__
        is_mcp_retriever = "mcpretriever" in retriever_name.lower()

        self.logger.info(f"Searching with {retriever_name} for query: {query}")

        try:
            # Instantiate the retriever
            retriever_instance = retriever(
                query=query,
                headers=self.researcher.headers,
                query_domains=self.researcher.query_domains,
                websocket=self.researcher.websocket if is_mcp_retriever else None,
                researcher=self.researcher if is_mcp_retriever else None
            )

            # Log MCP server configurations if using MCP retriever
            if is_mcp_retriever and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_retrieval",
                    f"🔌 Consulting MCP server(s) for information on: {query}",
                    self.researcher.websocket,
                )

            # Perform the search
            if hasattr(retriever_instance, 'search'):
                results = retriever_instance.search(
                    max_results=self.researcher.cfg.max_search_results_per_query
                )

                # Log result information
                if results:
                    result_count = len(results)
                    self.logger.info(f"Received {result_count} results from {retriever_name}")

                    # Special logging for MCP retriever
                    if is_mcp_retriever:
                        if self.researcher.verbose:
                            await stream_output(
                                "logs",
                                "mcp_results",
                                f"✓ Retrieved {result_count} results from MCP server",
                                self.researcher.websocket,
                            )

                        # Log result details
                        for i, result in enumerate(results[:3]):  # Log first 3 results
                            title = result.get("title", "No title")
                            url = result.get("href", "No URL")
                            content_length = len(result.get("body", "")) if result.get("body") else 0
                            self.logger.info(f"MCP result {i+1}: '{title}' from {url} ({content_length} chars)")

                        if result_count > 3:
                            self.logger.info(f"... and {result_count - 3} more MCP results")
                else:
                    self.logger.info(f"No results returned from {retriever_name}")
                    if is_mcp_retriever and self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_no_results",
                            f"ℹ️ No relevant information found from MCP server for: {query}",
                            self.researcher.websocket,
                        )

                return results
            else:
                self.logger.error(f"Retriever {retriever_name} does not have a search method")
                return []
        except Exception as e:
            self.logger.error(f"Error searching with {retriever_name}: {str(e)}")
            if is_mcp_retriever and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_error",
                    f"❌ Error retrieving information from MCP server: {str(e)}",
                    self.researcher.websocket,
                )
            return []

    async def _extract_content(self, results):
        """
        Extract content from search results using the browser manager.

        Args:
            results: Search results

        Returns:
            list: Extracted content
        """
        self.logger.info(f"Extracting content from {len(results)} search results")

        # Get the URLs from the search results
        urls = []
        for result in results:
            if isinstance(result, dict) and "href" in result:
                urls.append(result["href"])

        # Skip if no URLs found
        if not urls:
            return []

        # Make sure we don't visit URLs we've already visited
        new_urls = [url for url in urls if url not in self.researcher.visited_urls]

        # Return empty if no new URLs
        if not new_urls:
            return []

        # Scrape the content from the URLs
        scraped_content = await self.researcher.scraper_manager.browse_urls(new_urls)

        # Add the URLs to visited_urls
        self.researcher.visited_urls.update(new_urls)

        return scraped_content

    async def _summarize_content(self, query, content):
        """
        Summarize the extracted content.

        Args:
            query: The search query
            content: The extracted content

        Returns:
            str: Summarized content
        """
        self.logger.info(f"Summarizing content for query: {query}")

        # Skip if no content
        if not content:
            return ""

        # Summarize the content using the context manager
        summary = await self.researcher.context_manager.get_similar_content_by_query(
            query, content
        )

        return summary

    async def _update_search_progress(self, current, total):
        """
        Update the search progress.

        Args:
            current: Current number of sub-queries processed
            total: Total number of sub-queries
        """
        if self.researcher.verbose and self.researcher.websocket:
            progress = int((current / total) * 100)
            await stream_output(
                "logs",
                "research_progress",
                f"📊 Research Progress: {progress}%",
                self.researcher.websocket,
                True,
                {
                    "current": current,
                    "total": total,
                    "progress": progress
                }
            )
