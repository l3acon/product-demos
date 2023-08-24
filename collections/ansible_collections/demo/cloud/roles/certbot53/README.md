Role Name
=========

Certbot53

Requirements
------------

This role requires all the dependencies of the AWS collection/modules 
Additionally the python package:
  - cryptography


Role Variables
--------------

Dependencies
------------

- aws.aws


Example Playbook
----------------

     - name: ask ACME to renew certs via DNS TXT verification
       hosts: webservers
       become: true
       vars:
         hosted_zone: "my.hosted.zone.tld"
       tasks:
         - name: Make staging certs
           include_role:
             name: certbot53
           vars:
             hostname: myhost
             route53_zone: "{{ hosted_zone }}"
     
         - name: Make production certs
           include_role:
             name: certbot53
           vars:
             acme_env: production
             acme_directory: "https://acme-v02.api.letsencrypt.org/directory"
             hostname: myhost
             route53_zone: "{{ hosted_zone }}"
     
         - name: install certs
           ansible.builtin.file:
             src: "/root/ansi-certs/{{fqdn}}"
             dest: "/etc/gitlab/ssl/{{fqdn}}"
             owner: root
             group: root
             state: link
     

License
-------

BSD

Author Information
------------------

An optional section for the role authors to include contact information, or a website (HTML is not allowed).
