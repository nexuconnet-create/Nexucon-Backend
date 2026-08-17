from rest_framework import serializers
from .models import Project, ProjectMilestone, ProjectProfessional, ProjectDocument

class ProjectMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMilestone
        fields = '__all__'

class ProjectProfessionalSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectProfessional
        fields = ['id', 'name', 'organization', 'license_number', 'email', 'phone', 'role']
        
class ProjectDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectDocument
        fields = ['id', 'document_type', 'file', 'name', 'uploaded_at']

class ProjectSerializer(serializers.ModelSerializer):
    milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    professionals = ProjectProfessionalSerializer(many=True, required=False)
    project_documents = ProjectDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = Project
        fields = '__all__'

    def create(self, validated_data):
        professionals_data = validated_data.pop('professionals', [])
        project = Project.objects.create(**validated_data)
        
        for prof_data in professionals_data:
            ProjectProfessional.objects.create(project=project, **prof_data)
            
        return project

    def update(self, instance, validated_data):
        professionals_data = validated_data.pop('professionals', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if professionals_data is not None:
            instance.professionals.all().delete()
            for prof_data in professionals_data:
                ProjectProfessional.objects.create(project=instance, **prof_data)
                
        return instance
