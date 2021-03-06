import docker
import json
import subprocess
from synapseclient import File, Folder
import synapseutils as synu
import zipfile
import os

#Synapse Id of Challenge
CHALLENGE_SYN_ID = "syn1235"
#Synapse Id of directory that you want the log files to go into
CHALLENGE_LOG_FOLDER = "syn12345"
CHALLENGE_PREDICTION_FOLDER = "syn1235"

## Name of your challenge, defaults to the name of the challenge's project
CHALLENGE_NAME = "Example Synapse Challenge"

## Synapse user IDs of the challenge admins who will be notified by email
## about errors in the scoring script
ADMIN_USER_IDS = ['123234']


config_evaluations = [
#Sub-Challenge 1 (12345)
#Sub-Challenge 2 (23456)
    {
        'id':12345,
        'score_sh':'/score_sc1.sh'
    },
    {
        'id':23456,
        'score_sh':'/score_sc2.sh'
    }

]

config_evaluations_map = {ev['id']:ev for ev in config_evaluations}

def getBearerTokenURL(dockerRequestURL, user, password):
    initialReq = requests.get(dockerRequestURL)
    auth_headers = initialReq.headers['Www-Authenticate'].replace('"','').split(",")
    for head in auth_headers:
        if head.startswith("Bearer realm="):
            bearerRealm = head.split('Bearer realm=')[1]
        elif head.startswith('service='):
            service = head.split('service=')[1]
        elif head.startswith('scope='):
            scope = head.split('scope=')[1]
    return("{0}?service={1}&scope={2}".format(bearerRealm,service,scope))

def getAuthToken(dockerRequestURL, user, password):
    bearerTokenURL = getBearerTokenURL(dockerRequestURL, user, password)
    auth = base64.b64encode(user + ":" + password)
    bearerTokenRequest = requests.get(bearerTokenURL,
        headers={'Authorization': 'Basic %s' % auth})
    return(bearerTokenRequest.json()['token'])

def zipdir(path, ziph):
	# ziph is zipfile handle
	for root, dirs, files in os.walk(path):
		for file in files:
			ziph.write(os.path.join(root, file),os.path.join(root, file).replace(path+"/",""))

def dockerValidate(submission, syn, user, password):
    submissionJson = json.loads(submission['entityBundleJSON'])
    assert submissionJson['entity'].get('repositoryName') is not None, "Must submit a docker container"
    dockerRepo = submissionJson['entity']['repositoryName'].replace("docker.synapse.org/","")
    #assert dockerRepo.startswith("docker.synapse.org")
    assert submission.get('dockerDigest') is not None, "Must submit a docker container with a docker sha digest"
    dockerDigest = submission['dockerDigest']
    index_endpoint = 'https://docker.synapse.org'
    #dockerImage = dockerRepo + "@" + dockerDigest

    #Check if docker is able to be pulled
    dockerRequestURL = '{0}/v2/{1}/manifests/{2}'.format(index_endpoint, dockerRepo, dockerDigest)
    token = getAuthToken(dockerRequestURL, user, password)

    resp = requests.get(dockerRequestURL,
                        headers={'Authorization': 'Bearer %s' % token})
    assert resp.status_code == 200, "Docker image + sha digest must exist"
    
    #Must check docker image size
    #Synapse docker registry
    dockerSize = sum([layer['size'] for layer in resp.json()['layers']])
    assert dockerSize/1000000000.0 < 1000, "Docker image must be less than a teribyte"

    #Send email to me if harddrive is full 
    #should be stateless, if there needs to be code changes to the docker agent
    preds = synu.walk(syn, CHALLENGE_PREDICTION_FOLDER)
    predFolders = preds.next()[1]
    predSynId = [synId for name, synId in predFolders if str(submission.id) == name]

    logs = synu.walk(syn, CHALLENGE_LOG_FOLDER)
    logsFolders = logs.next()[1]
    logsSynId = [synId for name, synId in logsFolders if str(submission.id) == name]

    if len(predSynId) == 0:
        predFolder = syn.store(Folder(submission.id, parent = CHALLENGE_PREDICTION_FOLDER))
        predFolder = predFolder.id
    else:
        predFolder = predSynId[0]
    if len(logsSynId) == 0:
        logFolder = syn.store(Folder(submission.id, parent = CHALLENGE_LOG_FOLDER))
        logFolder = logFolder.id
        for participant in submission.contributors:
            if participant['principalId'] in ADMIN_USER_IDS: 
                access = ['CREATE', 'READ', 'DOWNLOAD', 'UPDATE', 'DELETE', 'CHANGE_PERMISSIONS', 'MODERATE', 'CHANGE_SETTINGS']
            else:
                access = ['READ','DOWNLOAD']
            #Comment set permissions out if you don't want to allow participants to see the pred files
            #syn.setPermissions(predFolder, principalId = participant['principalId'], accessType = access)
            syn.setPermissions(logFolder, principalId = participant['principalId'], accessType = access)
    else:
        logFolder = logsSynId[0]    
        #Add more message if you want to return the prediction files
    return(True, "Your submission has been validated!  As your submission is being ran, please go here: https://www.synapse.org/#!Synapse:%s to check on your log file." % logFolder)


def dockerRun(submission, scoring_sh, syn, client):

    #These are the volumes that you want to mount onto your docker container
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),submission.id)
    TESTDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),'test-data')
    #These are the locations on the docker that you want your mounted volumes to be + permissions in docker (ro, rw)
    #It has to be in this format '/output:rw'
    MOUNTED_VOLUMES = {OUTPUT_DIR:'/output:rw',
                       TESTDATA_DIR:'/test-data:ro'}
    #All mounted volumes here in a list
    ALL_VOLUMES = [OUTPUT_DIR,TESTDATA_DIR]

    #Make a file view of the prediction folder
    allLogs = synu.walk(syn, CHALLENGE_LOG_FOLDER)
    logFolder = allLogs.next()
    logFolderId = [synId for name, synId in logFolder[1] if name == submission.id][0]
    
    allPreds = synu.walk(syn, CHALLENGE_PREDICTION_FOLDER)
    predFolder = allPreds.next()
    predFolderId = [synId for name, synId in predFolder[1] if name == submission.id][0]

    dockerDigest = submission.get('dockerDigest')
    submissionJson = json.loads(submission['entityBundleJSON'])
    dockerRepo = submissionJson['entity']['repositoryName']
    dockerImage = dockerRepo + "@" + dockerDigest

    if not os.path.exists(OUTPUT_DIR):
        os.mkdir(OUTPUT_DIR)
        
	#Mount volumes
	volumes = {}
	for vol in ALL_VOLUMES:
		volumes[vol] = {'bind': MOUNTED_VOLUMES[vol].split(":")[0], 'mode': MOUNTED_VOLUMES[vol].split(":")[1]}

	# Run docker image
    errors = None
    try:
	   container = client.containers.run(dockerRepo, scoring_sh, detach=True, volumes = volumes, name=submission.id + "_t" + str(int(time.time())), network_disabled=True)
    except docker.errors.APIError as e:
        container = None
        errors = str(e) + "\n"

    #Create log file
    logFileName = submission.id + "_log.txt"
    logSynId = None
    #Create the logfile
    openLog = open(logFileName,'w').close()
    #While docker is still running (the docker python client doesn't update status)
    #Add sleeps
    if container is not None:
        while subprocess.Popen(['docker','inspect','-f','{{.State.Running}}',container.name],stdout = subprocess.PIPE).communicate()[0] == "true\n":
            logFileText = container.logs()
            with open(logFileName,'w') as logFile:
                logFile.write(logFileText)
            statinfo = os.stat(logFileName)
            #Only store log file if > 0bytes
            if statinfo.st_size > 0 and statinfo.st_size/1000.0 <= 50:
                ent = File(logFileName, parent = logFolderId)
                try:
                    logs = syn.store(ent)
                    logSynId = logs.id
                except synapseclient.exceptions.SynapseHTTPError as e:
                    pass
            time.sleep(60)

        #Must run again to make sure all the logs are captured
        logFileText = container.logs()
        with open(logFileName,'w') as logFile:
            logFile.write(logFileText)
        statinfo = os.stat(logFileName)
        #Only store log file if > 0bytes
        if statinfo.st_size > 0 and statinfo.st_size/1000.0 <= 50:
            ent = File(logFileName, parent = logFolderId)
            try:
                logs = syn.store(ent)
                logSynId = logs.id
            except synapseclient.exceptions.SynapseHTTPError as e:
                pass
        container.remove()
        try:
            client.images.remove(dockerImage)
        except:
            print("Unable to remove image")

    statinfo = os.stat(logFileName)
    if statinfo.st_size == 0:
        with open(logFileName,'w') as logFile:
            if errors is not None:
                logFile.write(errors)
            else:
                logFile.write("No Logs, or logs exceed size limit")
            logFile.flush()
            ent = File(logFileName, parent = logFolderId)
            try:
                logs = syn.store(ent)
                logSynId = logs.id
            except synapseclient.exceptions.SynapseHTTPError as e:
                pass

    if logSynId is None:
        logFile = synu.walk(syn, logFolderId)
        logFiles = logFile.next()
        logSynId = logFiles[2][0][1]
        
    #Zip up predictions and store it into CHALLENGE_PREDICTIONS_FOLDER
    if len(os.listdir(OUTPUT_DIR)) > 0:
        zipf = zipfile.ZipFile(submission.id + '_predictions.zip', 'w', zipfile.ZIP_DEFLATED)
        zipdir(OUTPUT_DIR, zipf)
        zipf.close()

        ent = File(submission.id + '_predictions.zip', parent = predFolderId)
        predictions = syn.store(ent)
        prediction_synId = predictions.id
        os.system("rm -rf %s" % OUTPUT_DIR)
        os.remove(submission.id + '_predictions.zip')
    else:
        prediction_synId = None
    #Remove log file and prediction file
    os.remove(logFileName)
    return(prediction_synId, logSynId)



def validate_docker(evaluation, submission, syn, client, user, password):
	"""
	Find the right validation function and validate the submission.

	:returns: (True, message) if validated, (False, message) if
			  validation fails or throws exception
	"""
    config = config_evaluations_map[int(evaluation.id)]

    results = dockerValidate(submission, syn, user, password)
	return(results)


def run_docker(evaluation, submission):
	"""
	Find the right scoring function and score the submission

	:returns: (score, message) where score is a dict of stats and message
			  is text for display to user
	"""

    config = config_evaluations_map[int(evaluation.id)]
    prediction_synId, log_synId =  dockerRun(submission,config['score_sh'], syn, client)
    if prediction_synId is not None:
        #Comment top line if you don't want to return the synId of prediction file
        #message = "You can find your prediction file here: https://www.synapse.org/#!Synapse:%s" % prediction_synId
        message = "Your prediction file has been stored, but you will not have access to it."
    else:
        message = "No prediction file generated, please check your log file: https://www.synapse.org/#!Synapse:%s" % log_synId
    return (dict(PREDICTION_FILE=prediction_synId, LOG_FILE = log_synId), message)

