"""
The heart of the app - manages jobs and workers
"""
import signal
import time

from backend.lib.proxied_requests import DelegatedRequestHandler
from common.lib.exceptions import JobClaimedException


class WorkerManager:
	"""
	Manages the job queue and worker pool
	"""
	queue = None
	db = None
	log = None
	modules = None
	proxy_delegator = None

	worker_pool = {}
	job_mapping = {}
	pool = []
	looping = True
	unknown_jobs = set()

	def __init__(self, queue, database, logger, modules, as_daemon=True):
		"""
		Initialize manager

		:param queue:  Job queue
		:param database:  Database handler
		:param logger:  Logger object
		:param modules:  Modules cache via ModuleLoader()
		:param bool as_daemon:  Whether the manager is being run as a daemon
		"""
		self.queue = queue
		self.db = database
		self.log = logger
		self.modules = modules
		self.proxy_delegator = DelegatedRequestHandler(self.log, self.modules.config)

		if as_daemon:
			signal.signal(signal.SIGTERM, self.abort)
			signal.signal(signal.SIGINT, self.request_interrupt)

		# datasources are initialized here; the init_datasource functions found in their respective __init__.py files
		# are called which, in the case of scrapers, also adds the scrape jobs to the queue.
		self.validate_datasources()

		# queue jobs for workers that always need one
		for worker_name, worker in self.modules.workers.items():
			if hasattr(worker, "ensure_job"):
				# ensure_job is a class method that returns a dict with job parameters if job should be added
				# pass config for some workers (e.g., web studies extensions)
				try:
					job_params = worker.ensure_job(config=self.modules.config)
				except Exception as e:
					self.log.error(f"Error while ensuring job for worker {worker_name}: {e}")
					job_params = None
				
				if job_params:
					self.queue.add_job(jobtype=worker_name, **job_params)


		self.log.info("4CAT Started")

		# flush module collector log buffer
		# the logger is not available when this initialises
		# but it is now!
		if self.modules.log_buffer:
			self.log.warning(self.modules.log_buffer)
			self.modules.log_buffer = ""

		# it's time
		self.loop()

	def delegate(self):
		"""
		Delegate work

		Checks for open jobs, and then passes those to dedicated workers, if
		slots are available for those workers.
		"""
		jobs = self.queue.get_all_jobs()

		num_active = sum([len(self.worker_pool[jobtype]) for jobtype in self.worker_pool])
		self.log.debug("Running workers: %i" % num_active)

		# clean up workers that have finished processing
		for jobtype in self.worker_pool:
			all_workers = self.worker_pool[jobtype]
			for worker in all_workers:
				if not worker.is_alive():
					self.log.debug(f"Terminating worker {worker.job.data['jobtype']}/{worker.job.data['remote_id']}")
					worker.join()
					self.worker_pool[jobtype].remove(worker)

			del all_workers

		# check if workers are available for unclaimed jobs
		for job in jobs:
			jobtype = job.data["jobtype"]

			if jobtype in self.modules.workers:
				worker_class = self.modules.workers[jobtype]
				if jobtype not in self.worker_pool:
					self.worker_pool[jobtype] = []

				# if a job is of a known type, and that job type has open
				# worker slots, start a new worker to run it
				if len(self.worker_pool[jobtype]) < worker_class.max_workers:
					try:
						job.claim()
						worker = worker_class(logger=self.log, manager=self, job=job, modules=self.modules)
						worker.start()
						self.log.info(f"Starting new worker of for job {job.data['jobtype']}/{job.data['remote_id']}")
						self.worker_pool[jobtype].append(worker)
					except JobClaimedException:
						# it's fine
						pass
			else:
				if jobtype not in self.unknown_jobs:
					self.log.error("Unknown job type: %s" % jobtype)
					self.unknown_jobs.add(jobtype)

		time.sleep(1)

	def loop(self):
		"""
		Main loop

		Constantly delegates work, until no longer looping, after which all
		workers are asked to stop their work. Once that has happened, the loop
		properly ends.
		"""
		while self.looping:
			try:
				self.delegate()
			except KeyboardInterrupt:
				self.looping = False

		self.log.info("Telling all workers to stop doing whatever they're doing...")

		# request shutdown from all workers except the API
		# this allows us to use the API to figure out if a certain worker is
		# hanging during shutdown, for example
		for jobtype in self.worker_pool:
			if jobtype == "api":
				continue

			for worker in self.worker_pool[jobtype]:
				if hasattr(worker, "request_interrupt"):
					worker.request_interrupt()
				else:
					worker.abort()

		# wait for all workers that we just asked to quit to finish
		self.log.info("Waiting for all workers to finish...")
		for jobtype in self.worker_pool:
			if jobtype == "api":
				continue
			for worker in self.worker_pool[jobtype]:
				self.log.info("Waiting for worker %s..." % jobtype)
				worker.join()

		# shut down API last
		for worker in self.worker_pool.get("api", []):
			worker.request_interrupt()
			worker.join()

		# abort
		time.sleep(1)
		self.log.info("Bye!")

	def validate_datasources(self):
		"""
		Validate data sources

		Logs warnings if not all information is precent for the configured data
		sources.
		"""

		for datasource in self.modules.datasources:
			if datasource + "-search" not in self.modules.workers and datasource + "-import" not in self.modules.workers:
				self.log.error("No search worker defined for datasource %s or its modules are missing. Search queries will not be executed." % datasource)

			self.modules.datasources[datasource]["init"](self.db, self.log, self.queue, datasource, self.modules.config)

	def abort(self, signal=None, stack=None):
		"""
		Stop looping the delegator, clean up, and prepare for shutdown
		"""
		self.log.info("Received SIGTERM")

		# cancel all interruptible postgres queries
		# this needs to be done before we stop looping since after that no new
		# jobs will be claimed, and needs to be done here because the worker's
		# own database connection is busy executing the query that it should
		# cancel! so we can't use it to update the job and make it get claimed
		for job in self.queue.get_all_jobs("cancel-pg-query", restrict_claimable=False):
			# this will make all these jobs immediately claimable, i.e. queries
			# will get cancelled asap
			self.log.debug("Cancelling interruptable Postgres queries for connection %s..." % job.data["remote_id"])
			job.claim()
			job.release(delay=0, claim_after=0)

		# wait until all cancel jobs are completed
		while self.queue.get_all_jobs("cancel-pg-query", restrict_claimable=False):
			time.sleep(0.25)

		# now stop looping (i.e. accepting new jobs)
		self.looping = False

	def request_interrupt(self, interrupt_level, job=None, remote_id=None, jobtype=None):
		"""
		Interrupt a job

		This method can be called via e.g. the API, to interrupt a specific
		job's worker. The worker can be targeted either with a Job object or
		with a combination of job type and remote ID, since those uniquely
		identify a job.

		:param int interrupt_level:  Retry later or cancel?
		:param Job job:  Job object to cancel worker for
		:param str remote_id:  Remote ID for worker job to cancel
		:param str jobtype:  Job type for worker job to cancel
		"""

		# find worker for given job
		if job:
			jobtype = job.data["jobtype"]

		if jobtype not in self.worker_pool:
			# no jobs of this type currently known
			return

		for worker in self.worker_pool[jobtype]:
			if (job and worker.job.data["id"] == job.data["id"]) or (worker.job.data["jobtype"] == jobtype and worker.job.data["remote_id"] == remote_id):
				# first cancel any interruptable queries for this job's worker
				while True:
					active_queries = self.queue.get_all_jobs("cancel-pg-query", remote_id=worker.db.appname, restrict_claimable=False)
					if not active_queries:
						# all cancellation jobs have been run
						break

					for cancel_job in active_queries:
						if cancel_job.is_claimed:
							continue

						# this will make the job be run asap
						cancel_job.claim()
						cancel_job.release(delay=0, claim_after=0)

					# give the cancel job a moment to run
					time.sleep(0.25)

				# now all queries are interrupted, formally request an abort
				self.log.info(f"Requesting interrupt of job {worker.job.data['id']} ({worker.job.data['jobtype']}/{worker.job.data['remote_id']})")
				worker.request_interrupt(interrupt_level)
				return